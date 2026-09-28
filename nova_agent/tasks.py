"""Task lifecycle: workspace setup, background execution, event log, diff."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import Agent, AgentStopped
from .config import Config
from .sandbox import Sandbox, make_sandbox
from .tools import ToolExecutor

ACTIVE = {"queued", "running"}
DELTAS = {"text_delta", "thinking_delta"}
REPO_URL = re.compile(r"^https://[\w.-]+(:\d+)?/[\w./~-]+$")

AgentFactory = Callable[[Config, ToolExecutor, Callable, threading.Event], Agent]


def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", "protocol.ext.allow=never", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


@dataclass
class Task:
    id: str
    prompt: str
    repo_url: str | None
    branch: str | None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    summary: str = ""
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    # Runtime-only state (not persisted).
    messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    sandbox: Sandbox | None = field(default=None, repr=False)

    @property
    def title(self) -> str:
        first = self.prompt.strip().splitlines()[0] if self.prompt.strip() else "Untitled task"
        return first[:80]

    def to_json(self, with_events: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "error": self.error,
            "can_follow_up": bool(self.messages) and self.status not in ACTIVE,
        }
        if with_events:
            # Streaming deltas only matter to a live viewer; the complete block
            # is logged separately right after them.
            data["events"] = [e for e in self.events if e["type"] not in DELTAS]
        return data


class TaskManager:
    def __init__(self, config: Config, agent_factory: AgentFactory | None = None):
        self.config = config
        self.agent_factory = agent_factory or (lambda cfg, ex, emit, cancel: Agent(cfg, ex, emit, cancel))
        self.tasks: dict[str, Task] = {}
        self.lock = threading.RLock()
        self._conds: dict[str, threading.Condition] = {}
        self.pool = ThreadPoolExecutor(max_workers=config.max_concurrent, thread_name_prefix="nova-task")
        config.workspaces_dir.mkdir(parents=True, exist_ok=True)
        config.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        for file in sorted(self.config.tasks_dir.glob("*.json")):
            try:
                data = json.loads(file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            task = Task(
                id=data["id"],
                prompt=data["prompt"],
                repo_url=data.get("repo_url"),
                branch=data.get("branch"),
                status=data.get("status", "failed"),
                created_at=data.get("created_at", time.time()),
                updated_at=data.get("updated_at", time.time()),
                summary=data.get("summary", ""),
                error=data.get("error", ""),
                events=data.get("events", []),
            )
            if task.status in ACTIVE:
                task.status = "failed"
                task.error = "The server restarted while this task was running."
            self.tasks[task.id] = task

    def _save(self, task: Task) -> None:
        path = self.config.tasks_dir / f"{task.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task.to_json(), default=str))
        tmp.replace(path)

    # -- public API ---------------------------------------------------------

    def list(self) -> list[Task]:
        with self.lock:
            return sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def workspace(self, task: Task) -> Path:
        return self.config.workspaces_dir / task.id

    def create(self, prompt: str, repo_url: str | None = None, branch: str | None = None) -> Task:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")
        repo_url = (repo_url or "").strip() or None
        branch = (branch or "").strip() or None
        if repo_url and not REPO_URL.match(repo_url):
            raise ValueError("repo_url must be an https:// git URL")
        if branch and not re.fullmatch(r"[\w./-]+", branch):
            raise ValueError("invalid branch name")
        task = Task(id=uuid.uuid4().hex[:12], prompt=prompt, repo_url=repo_url, branch=branch)
        task.messages.append({"role": "user", "content": prompt})
        with self.lock:
            self.tasks[task.id] = task
        self._emit(task, "user", {"text": prompt})
        self.pool.submit(self._run, task, True)
        return task

    def follow_up(self, task_id: str, text: str) -> Task:
        task = self._require(task_id)
        text = text.strip()
        if not text:
            raise ValueError("message is required")
        with self.lock:
            if task.status in ACTIVE:
                raise ValueError("task is still running")
            if not task.messages:
                raise ValueError("this task's conversation is no longer in memory (server restarted)")
            task.cancel = threading.Event()
            task.messages.append({"role": "user", "content": text})
            task.status, task.error = "queued", ""
        self._emit(task, "user", {"text": text})
        self.pool.submit(self._run, task, False)
        return task

    def cancel(self, task_id: str) -> Task:
        task = self._require(task_id)
        task.cancel.set()
        return task

    def delete(self, task_id: str) -> None:
        task = self._require(task_id)
        task.cancel.set()
        with self.lock:
            self.tasks.pop(task_id, None)
        if task.sandbox:
            task.sandbox.close()
        shutil.rmtree(self.workspace(task), ignore_errors=True)
        (self.config.tasks_dir / f"{task.id}.json").unlink(missing_ok=True)

    def diff(self, task_id: str) -> str:
        task = self._require(task_id)
        ws = self.workspace(task)
        if not (ws / ".git").exists():
            return ""
        # Mark new files as intent-to-add so they show up in the diff.
        git(ws, "add", "--intent-to-add", "--all", check=False)
        return git(ws, "diff", "--no-color", "--no-ext-diff", check=False)

    def commit(self, task_id: str, message: str | None, push: bool) -> dict[str, str]:
        task = self._require(task_id)
        if task.status in ACTIVE:
            raise ValueError("task is still running")
        ws = self.workspace(task)
        branch = f"allfixx/{task.id}"
        git(ws, "checkout", "-B", branch)
        git(ws, "add", "--all")
        if not git(ws, "status", "--porcelain").strip():
            raise ValueError("there are no changes to commit")
        git(
            ws, "-c", "user.name=Allfixx Code", "-c", "user.email=allfixx-code@localhost",
            "commit", "-m", (message or task.title).strip() or task.title,
        )
        sha = git(ws, "rev-parse", "HEAD").strip()
        result = {"branch": branch, "commit": sha}
        if push:
            if not task.repo_url:
                raise ValueError("task has no remote repository to push to")
            git(ws, "push", "-u", "origin", branch)
            result["pushed"] = "true"
        self._emit(task, "commit", result)
        return result

    def wait_events(self, task: Task, after: int, timeout: float) -> list[dict[str, Any]]:
        """Block until events newer than `after` exist or the timeout passes."""
        deadline = time.time() + timeout
        with self._cond(task):
            while len(task.events) <= after and time.time() < deadline:
                self._cond(task).wait(timeout=max(0.0, deadline - time.time()))
            return task.events[after:]

    def shutdown(self) -> None:
        for task in self.list():
            task.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for task in self.list():
            if task.sandbox:
                task.sandbox.close()

    # -- internals ----------------------------------------------------------

    def _cond(self, task: Task) -> threading.Condition:
        with self.lock:
            return self._conds.setdefault(task.id, threading.Condition())

    def _require(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _emit(self, task: Task, kind: str, data: dict[str, Any]) -> None:
        cond = self._cond(task)
        with cond:
            task.events.append({"seq": len(task.events), "ts": time.time(), "type": kind, "data": data})
            task.updated_at = time.time()
            cond.notify_all()
        if kind not in DELTAS:
            self._save(task)

    def _set_status(self, task: Task, status: str, **fields: str) -> None:
        task.status = status
        for key, value in fields.items():
            setattr(task, key, value)
        self._emit(task, "status", {"status": status, **fields})

    def _prepare_workspace(self, task: Task) -> None:
        ws = self.workspace(task)
        if ws.exists():
            return
        if task.repo_url:
            self._emit(task, "log", {"text": f"Cloning {task.repo_url} ..."})
            args = ["clone", "--depth", "50"]
            if task.branch:
                args += ["--branch", task.branch]
            git(self.config.workspaces_dir, *args, "--", task.repo_url, str(ws))
        else:
            ws.mkdir(parents=True)
            git(ws, "init", "-q")
            git(
                ws, "-c", "user.name=Allfixx Code", "-c", "user.email=allfixx-code@localhost",
                "commit", "-q", "--allow-empty", "-m", "Empty workspace",
            )

    def _run(self, task: Task, first: bool) -> None:
        self._set_status(task, "running")
        try:
            if first:
                self._prepare_workspace(task)
            if task.sandbox is None:
                self._emit(task, "log", {"text": f"Starting {self.config.sandbox} sandbox ..."})
                task.sandbox = make_sandbox(self.config, self.workspace(task), task.id)
            agent = self.agent_factory(
                self.config,
                ToolExecutor(task.sandbox),
                lambda kind, data: self._emit(task, kind, data),
                task.cancel,
            )
            summary = agent.run(task.messages)
            self._set_status(task, "completed", summary=summary)
        except AgentStopped:
            self._set_status(task, "cancelled")
        except Exception as exc:
            self._set_status(task, "failed", error=f"{exc}")
        finally:
            # Containers are not kept between turns; a follow-up message starts
            # a fresh one on the same workspace.
            if task.sandbox is not None:
                task.sandbox.close()
                task.sandbox = None
