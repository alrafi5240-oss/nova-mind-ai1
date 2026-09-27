import pytest

from nova_agent.sandbox import LocalSandbox
from nova_agent.tools import ToolError, ToolExecutor


@pytest.fixture
def ex(tmp_path):
    sb = LocalSandbox(tmp_path, timeout=5)
    yield ToolExecutor(sb)
    sb.close()


def edit(ex, **kw):
    return ex.execute("str_replace_based_edit_tool", kw)


def test_create_view_replace_insert(ex, tmp_path):
    edit(ex, command="create", path="src/app.py", file_text="a = 1\nb = 2\n")
    assert (tmp_path / "src/app.py").read_text() == "a = 1\nb = 2\n"
    assert "     2\tb = 2" in edit(ex, command="view", path="src/app.py")
    assert edit(ex, command="view", path="src/app.py", view_range=[2, -1]).strip() == "2\tb = 2"
    edit(ex, command="str_replace", path=f"{tmp_path}/src/app.py", old_str="b = 2", new_str="b = 3")
    edit(ex, command="insert", path="src/app.py", insert_line=0, insert_text="import os")
    assert (tmp_path / "src/app.py").read_text() == "import os\na = 1\nb = 3\n"
    assert "src/app.py" in edit(ex, command="view", path=".")


def test_str_replace_requires_unique_match(ex):
    edit(ex, command="create", path="f.txt", file_text="x\nx\n")
    with pytest.raises(ToolError, match="2 times"):
        edit(ex, command="str_replace", path="f.txt", old_str="x", new_str="y")
    with pytest.raises(ToolError, match="not found"):
        edit(ex, command="str_replace", path="f.txt", old_str="zzz", new_str="y")


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "a/../../x"])
def test_paths_cannot_escape_workspace(ex, path):
    with pytest.raises(ToolError, match="outside the workspace"):
        edit(ex, command="create", path=path, file_text="nope")


def test_symlink_escape_is_blocked(ex, tmp_path):
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(ToolError, match="outside the workspace"):
        edit(ex, command="view", path="link/hostname")


def test_bash_keeps_state_and_reports_exit_codes(ex):
    ex.execute("bash", {"command": "mkdir sub && cd sub && export FOO=bar"})
    assert ex.execute("bash", {"command": "pwd; echo $FOO"}).endswith("sub\nbar")
    assert "[exit code 3]" in ex.execute("bash", {"command": "echo hi; exit 3"})
    # The shell died with `exit`; the next command transparently gets a new one.
    assert ex.execute("bash", {"command": "echo back"}) == "back"
    assert ex.execute("bash", {"command": "cat"}) == "(no output)"  # stdin is not the control channel


def test_bash_timeout_restarts_shell(tmp_path):
    sb = LocalSandbox(tmp_path, timeout=1)
    ex = ToolExecutor(sb)
    assert "timed out" in ex.execute("bash", {"command": "sleep 5"})
    assert ex.execute("bash", {"command": "echo ok"}) == "ok"
    sb.close()


def test_bash_does_not_leak_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    sb = LocalSandbox(tmp_path, timeout=5)
    assert ToolExecutor(sb).execute("bash", {"command": "echo ${ANTHROPIC_API_KEY:-unset}"}) == "unset"
    sb.close()
