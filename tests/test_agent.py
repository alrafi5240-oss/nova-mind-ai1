import threading

import pytest

from nova_agent.agent import Agent, AgentStopped
from nova_agent.config import Config
from nova_agent.sandbox import LocalSandbox
from nova_agent.tools import ToolExecutor

from .fakes import FakeClient, response, text, tool_use


def make(tmp_path, responses, cancel=None):
    events = []
    client = FakeClient(responses)
    sb = LocalSandbox(tmp_path, timeout=5)
    agent = Agent(
        Config(sandbox="local"),
        ToolExecutor(sb),
        lambda kind, data: events.append((kind, data)),
        cancel or threading.Event(),
        client=client,
    )
    return agent, client, events, sb


def test_loop_runs_tools_until_end_turn(tmp_path):
    agent, client, events, sb = make(tmp_path, [
        response([text("Creating the file."), tool_use("t1", "str_replace_based_edit_tool",
                  {"command": "create", "path": "hello.py", "file_text": "print('hi')\n"})], "tool_use"),
        response([tool_use("t2", "bash", {"command": "python3 hello.py"})], "tool_use"),
        response([text("Done: added hello.py.")]),
    ])
    messages = [{"role": "user", "content": "make hello.py"}]
    assert agent.run(messages) == "Done: added hello.py."
    sb.close()

    assert (tmp_path / "hello.py").exists()
    # Transcript is append-only: user, assistant, tool results, assistant, results, assistant.
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert messages[4]["content"][0] == {"type": "tool_result", "tool_use_id": "t2", "content": "hi"}
    kinds = [k for k, _ in events]
    assert kinds.count("tool_call") == 2 and kinds.count("tool_result") == 2
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call["fallbacks"] == "default"


def test_tool_errors_are_returned_to_claude(tmp_path):
    agent, client, events, sb = make(tmp_path, [
        response([tool_use("t1", "str_replace_based_edit_tool", {"command": "view", "path": "../x"})], "tool_use"),
        response([text("ok")]),
    ])
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)
    sb.close()
    result = messages[2]["content"][0]
    assert result["is_error"] is True and "outside the workspace" in result["content"]


def test_refusal_fails_the_task(tmp_path):
    agent, _, _, sb = make(tmp_path, [response([], "refusal")])
    with pytest.raises(RuntimeError, match="declined"):
        agent.run([{"role": "user", "content": "x"}])
    sb.close()


def test_cancel_stops_the_loop(tmp_path):
    cancel = threading.Event()
    cancel.set()
    agent, client, _, sb = make(tmp_path, [response([text("never")])], cancel)
    with pytest.raises(AgentStopped):
        agent.run([{"role": "user", "content": "x"}])
    assert client.calls == []
    sb.close()
