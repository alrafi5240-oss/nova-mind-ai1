import json
import time

from fastapi.testclient import TestClient

from nova_agent.agent import Agent
from nova_agent.config import Config
from nova_agent.server import create_app
from nova_agent.tasks import TaskManager

from .fakes import FakeClient, response, text, tool_use


def build(tmp_path, scripts, token=None):
    config = Config(sandbox="local", data_dir=tmp_path, api_token=token)
    scripts = list(scripts)

    def factory(cfg, executor, emit, cancel):
        return Agent(cfg, executor, emit, cancel, client=FakeClient(scripts.pop(0)))

    manager = TaskManager(config, agent_factory=factory)
    return TestClient(create_app(config, manager)), manager


def wait_done(client, task_id, headers=None):
    for _ in range(100):
        task = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_task_lifecycle_diff_follow_up_and_commit(tmp_path):
    client, manager = build(tmp_path, [
        [
            response([tool_use("t1", "str_replace_based_edit_tool",
                      {"command": "create", "path": "README.md", "file_text": "# Demo\n"})], "tool_use"),
            response([text("Added a README.")]),
        ],
        [
            response([tool_use("t2", "bash", {"command": "echo more >> README.md"})], "tool_use"),
            response([text("Extended it.")]),
        ],
    ])
    with client:
        assert client.get("/").status_code == 200
        created = client.post("/api/tasks", json={"prompt": "Add a README"})
        assert created.status_code == 201
        task_id = created.json()["id"]

        task = wait_done(client, task_id)
        assert task["status"] == "completed" and task["summary"] == "Added a README."
        assert all(e["type"] not in ("text_delta", "thinking_delta") for e in task["events"])
        assert "+# Demo" in client.get(f"/api/tasks/{task_id}/diff").json()["diff"]

        assert client.post(f"/api/tasks/{task_id}/messages", json={"message": "add more"}).status_code == 200
        task = wait_done(client, task_id)
        assert task["summary"] == "Extended it."
        assert "+more" in client.get(f"/api/tasks/{task_id}/diff").json()["diff"]

        sse = client.get(f"/api/tasks/{task_id}/events?follow=false").text
        events = [json.loads(line[6:]) for line in sse.splitlines() if line.startswith("data: ")]
        assert events[0]["type"] == "user" and events[-1]["data"]["status"] == "completed"
        assert "text_delta" in {e["type"] for e in events}  # live stream includes deltas

        commit = client.post(f"/api/tasks/{task_id}/commit", json={"message": "Add README"}).json()
        assert commit["branch"] == f"nova/{task_id}"
        assert client.post(f"/api/tasks/{task_id}/commit", json={}).status_code == 409  # nothing left

        assert [t["id"] for t in client.get("/api/tasks").json()] == [task_id]
        assert client.delete(f"/api/tasks/{task_id}").status_code == 204
        assert client.get(f"/api/tasks/{task_id}").status_code == 404


def test_validation_and_auth(tmp_path):
    client, _ = build(tmp_path, [], token="s3cret")
    with client:
        assert client.get("/api/tasks").status_code == 401
        auth = {"Authorization": "Bearer s3cret"}
        assert client.get("/api/tasks", headers=auth).status_code == 200
        assert client.get("/api/tasks?token=s3cret").status_code == 200
        bad = client.post("/api/tasks", json={"prompt": "x", "repo_url": "file:///etc"}, headers=auth)
        assert bad.status_code == 400
        assert client.post("/api/tasks", json={"prompt": "  "}, headers=auth).status_code == 400


def test_running_tasks_are_marked_failed_after_restart(tmp_path):
    (tmp_path / "tasks").mkdir(parents=True)
    (tmp_path / "tasks" / "abc.json").write_text(json.dumps({"id": "abc", "prompt": "p", "status": "running"}))
    manager = TaskManager(Config(sandbox="local", data_dir=tmp_path))
    task = manager.get("abc")
    assert task.status == "failed" and "restarted" in task.error
    assert task.to_json()["can_follow_up"] is False
