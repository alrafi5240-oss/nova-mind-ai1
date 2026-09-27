"""Handlers for Claude's Anthropic-defined bash and text editor tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sandbox import Sandbox, truncate

TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


class ToolError(Exception):
    """A tool failure reported back to Claude as an is_error tool_result."""


class ToolExecutor:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.root = sandbox.root

    def execute(self, name: str, tool_input: dict[str, Any]) -> str:
        if name == "bash":
            return self._bash(tool_input)
        if name == "str_replace_based_edit_tool":
            return self._editor(tool_input)
        raise ToolError(f"Unknown tool: {name}")

    # -- bash ---------------------------------------------------------------

    def _bash(self, tool_input: dict[str, Any]) -> str:
        if tool_input.get("restart"):
            self.sandbox.restart()
            return "Shell restarted."
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("bash requires a non-empty 'command'")
        code, output = self.sandbox.run(command)
        output = output.rstrip("\n")
        if code != 0:
            return f"{output}\n[exit code {code}]".lstrip("\n")
        return output or "(no output)"

    # -- text editor --------------------------------------------------------

    def resolve(self, raw_path: Any) -> Path:
        """Map a model-supplied path onto the workspace, refusing escapes."""
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolError("'path' is required")
        workdir = self.sandbox.workdir.rstrip("/")
        path = raw_path
        if path == workdir:
            path = "."
        elif path.startswith(workdir + "/"):
            path = path[len(workdir) + 1 :]
        elif path.startswith("/"):
            raise ToolError(f"Path {raw_path} is outside the workspace {workdir}")
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError(f"Path {raw_path} is outside the workspace {workdir}")
        return resolved

    def display(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        base = self.sandbox.workdir.rstrip("/")
        return base if rel == "." else f"{base}/{rel}"

    def _editor(self, tool_input: dict[str, Any]) -> str:
        command = tool_input.get("command")
        path = self.resolve(tool_input.get("path"))
        if command == "view":
            return self._view(path, tool_input.get("view_range"))
        if command == "create":
            text = tool_input.get("file_text")
            if not isinstance(text, str):
                raise ToolError("create requires 'file_text'")
            existed = path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return f"{'Overwrote' if existed else 'Created'} {self.display(path)}"
        if command == "str_replace":
            return self._str_replace(path, tool_input.get("old_str"), tool_input.get("new_str", ""))
        if command == "insert":
            return self._insert(path, tool_input.get("insert_line"), tool_input.get("insert_text"))
        raise ToolError(f"Unsupported editor command: {command!r}")

    def _read(self, path: Path) -> str:
        if not path.is_file():
            raise ToolError(f"File not found: {self.display(path)}")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{self.display(path)} is not a UTF-8 text file") from None

    def _view(self, path: Path, view_range: Any) -> str:
        if path.is_dir():
            entries: list[str] = []
            for child in sorted(path.iterdir()):
                if child.name in SKIP_DIRS:
                    continue
                entries.append(self.display(child) + ("/" if child.is_dir() else ""))
                if child.is_dir():
                    for grandchild in sorted(child.iterdir())[:50]:
                        if grandchild.name not in SKIP_DIRS:
                            entries.append(self.display(grandchild) + ("/" if grandchild.is_dir() else ""))
            return "\n".join(entries) or "(empty directory)"
        lines = self._read(path).splitlines()
        start, end = 1, len(lines)
        if view_range:
            if not (isinstance(view_range, list) and len(view_range) == 2):
                raise ToolError("view_range must be [start_line, end_line]")
            start = int(view_range[0])
            end = len(lines) if int(view_range[1]) == -1 else int(view_range[1])
            if start < 1 or end < start or start > max(len(lines), 1):
                raise ToolError(f"Invalid view_range {view_range} for a file with {len(lines)} lines")
        numbered = [f"{n:6}\t{lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1)]
        return truncate("\n".join(numbered)) or "(empty file)"

    def _str_replace(self, path: Path, old: Any, new: Any) -> str:
        if not isinstance(old, str) or not old:
            raise ToolError("str_replace requires a non-empty 'old_str'")
        if not isinstance(new, str):
            raise ToolError("'new_str' must be a string")
        content = self._read(path)
        count = content.count(old)
        if count == 0:
            raise ToolError(f"old_str was not found in {self.display(path)}")
        if count > 1:
            raise ToolError(
                f"old_str appears {count} times in {self.display(path)}; include more context to make it unique"
            )
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"Edited {self.display(path)}"

    def _insert(self, path: Path, line: Any, text: Any) -> str:
        if not isinstance(text, str):
            raise ToolError("insert requires 'insert_text'")
        lines = self._read(path).splitlines(keepends=True)
        if not isinstance(line, int) or not 0 <= line <= len(lines):
            raise ToolError(f"insert_line must be between 0 and {len(lines)}")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if not text.endswith("\n"):
            text += "\n"
        lines.insert(line, text)
        path.write_text("".join(lines), encoding="utf-8")
        return f"Inserted text after line {line} of {self.display(path)}"
