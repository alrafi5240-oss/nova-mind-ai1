"""FastAPI server: REST + Server-Sent Events API and the web UI."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import Config
from .tasks import TaskManager

STATIC = Path(__file__).parent / "static"


class CreateTask(BaseModel):
    prompt: str
    repo_url: str | None = None
    branch: str | None = None


class FollowUp(BaseModel):
    message: str


class Commit(BaseModel):
    message: str | None = None
    push: bool = False


def create_app(
    config: Config | None = None, manager: TaskManager | None = None, lan_url: str | None = None
) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = manager or TaskManager(config)
        yield
        app.state.manager.shutdown()

    app = FastAPI(title="NOVA Cloud Agent", lifespan=lifespan)

    def auth(request: Request) -> None:
        if not config.api_token:
            return
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, config.api_token):
            raise HTTPException(401, "invalid or missing token")

    def tasks(request: Request) -> TaskManager:
        return request.app.state.manager

    def find(request: Request, task_id: str):
        task = tasks(request).get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config", dependencies=[Depends(auth)])
    def get_config():
        return {
            "model": config.model,
            "effort": config.effort,
            "sandbox": config.sandbox,
            # Set when the server listens on the local network, so the UI can
            # show the address to open on a phone.
            "lan_url": lan_url,
        }

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    def list_tasks(request: Request):
        return [t.to_json(with_events=False) for t in tasks(request).list()]

    @app.post("/api/tasks", status_code=201, dependencies=[Depends(auth)])
    def create_task(body: CreateTask, request: Request):
        try:
            task = tasks(request).create(body.prompt, body.repo_url, body.branch)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return task.to_json()

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: str, request: Request):
        return find(request, task_id).to_json()

    @app.delete("/api/tasks/{task_id}", status_code=204, dependencies=[Depends(auth)])
    def delete_task(task_id: str, request: Request):
        find(request, task_id)
        tasks(request).delete(task_id)

    @app.post("/api/tasks/{task_id}/messages", dependencies=[Depends(auth)])
    def follow_up(task_id: str, body: FollowUp, request: Request):
        find(request, task_id)
        try:
            return tasks(request).follow_up(task_id, body.message).to_json(with_events=False)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    def cancel(task_id: str, request: Request):
        find(request, task_id)
        return tasks(request).cancel(task_id).to_json(with_events=False)

    @app.get("/api/tasks/{task_id}/diff", dependencies=[Depends(auth)])
    def diff(task_id: str, request: Request):
        find(request, task_id)
        return {"diff": tasks(request).diff(task_id)}

    @app.post("/api/tasks/{task_id}/commit", dependencies=[Depends(auth)])
    def commit(task_id: str, body: Commit, request: Request):
        find(request, task_id)
        try:
            return tasks(request).commit(task_id, body.message, body.push)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/tasks/{task_id}/events", dependencies=[Depends(auth)])
    async def events(task_id: str, request: Request, after: int = 0, follow: bool = True):
        task = find(request, task_id)
        manager = tasks(request)

        async def stream():
            cursor = max(0, after)
            while True:
                if await request.is_disconnected():
                    return
                batch = await asyncio.to_thread(manager.wait_events, task, cursor, 15.0 if follow else 0.0)
                for event in batch:
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
                cursor += len(batch)
                if not follow and cursor >= len(task.events):
                    return
                if not batch:
                    # Stays open after the task finishes so follow-ups stream too.
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def lan_ip() -> str | None:
    """The address other devices on the same network can reach us at."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packets are sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def main() -> None:
    import argparse
    import threading
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the NOVA cloud agent server")
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow phones on your Wi-Fi")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="open the web UI in your browser")
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    config = Config.from_env()

    if not is_loopback(args.host) and not config.api_token:
        sys.exit(
            "Refusing to listen on the network without an access token.\n"
            "Set NOVA_AGENT_TOKEN (for example in .env) or use --host 127.0.0.1."
        )
    if not port_free(args.host, args.port):
        sys.exit(f"Port {args.port} is already in use. Try: --port {args.port + 1}")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("Warning: ANTHROPIC_API_KEY is not set; tasks will fail until you add it to .env.")

    local_url = f"http://127.0.0.1:{args.port}"
    lan_url = None
    if args.host in ("0.0.0.0", "::"):
        ip = lan_ip()
        lan_url = f"http://{ip}:{args.port}" if ip else None

    print(f"\n  NOVA is running at {local_url}")
    if lan_url:
        print(f"  On your phone (same Wi-Fi): {lan_url}")
    print("  Press Ctrl+C to stop.\n")
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(local_url,)).start()

    uvicorn.run(create_app(config, lan_url=lan_url), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
