"""The agent loop: Claude plans, calls tools in the sandbox, and repeats."""

from __future__ import annotations

import threading
from typing import Any, Callable

import anthropic

from .config import Config
from .tools import TOOLS, ToolError, ToolExecutor

Emit = Callable[[str, dict[str, Any]], None]

SYSTEM_PROMPT = """\
You are Allfixx Code, an autonomous software engineering agent working in a cloud sandbox.
You are given a task on a code repository and work through it on your own until it
is done; nobody is watching live to answer questions mid-task.

Environment: {sandbox}. The repository is at {workdir} and is your working
directory. Use the bash tool to explore, install dependencies, run code and tests,
and use git to inspect history. Use the str_replace_based_edit_tool to read and
edit files.

How to work:
- Read the relevant code before changing it, and follow the conventions you find.
- Make the smallest change that fully solves the task.
- Verify your work: run the tests, linters or the program itself when that is
  possible, and fix what fails.
- Do not commit, push, or rewrite git history; the user reviews your diff first.
- If the task is ambiguous, pick the most reasonable interpretation, do it, and
  state the assumption in your summary.

When you are finished, reply with a short summary: what you changed and why, how
you verified it, and anything left undone.
"""


class AgentStopped(Exception):
    pass


class Agent:
    def __init__(
        self,
        config: Config,
        executor: ToolExecutor,
        emit: Emit,
        cancel: threading.Event,
        client: Any | None = None,
    ):
        self.config = config
        self.executor = executor
        self.emit = emit
        self.cancel = cancel
        self.client = client or anthropic.Anthropic()
        self.system = SYSTEM_PROMPT.format(
            sandbox=executor.sandbox.description, workdir=executor.sandbox.workdir
        )

    def run(self, messages: list[dict[str, Any]]) -> str:
        """Drive the loop until Claude ends its turn. Mutates `messages` in place
        (append-only) so a follow-up message can continue the same conversation.
        Returns Claude's final text."""
        for turn in range(self.config.max_turns):
            self._check_cancel()
            response = self._call(messages)
            # Append the full content (thinking blocks included) unchanged.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                reason = getattr(details, "explanation", None) or "the request was declined"
                raise RuntimeError(f"Claude declined this task: {reason}")

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "max_tokens" and not tool_uses:
                messages.append({"role": "user", "content": "Continue from where you stopped."})
                continue
            if response.stop_reason == "pause_turn":
                continue
            if not tool_uses:
                return _text_of(response.content)

            results = []
            for block in tool_uses:
                self._check_cancel()
                results.append(self._run_tool(block))
            messages.append({"role": "user", "content": results})

        raise RuntimeError(f"Stopped after {self.config.max_turns} turns without finishing")

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise AgentStopped()

    def _call(self, messages: list[dict[str, Any]]):
        with self.client.beta.messages.stream(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=self.system,
            messages=messages,
            tools=TOOLS,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.config.effort},
            # On a safety decline, let the API retry on its recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for event in stream:
                if self.cancel.is_set():
                    break
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        self.emit("text_delta", {"text": event.delta.text})
                    elif event.delta.type == "thinking_delta" and event.delta.thinking:
                        self.emit("thinking_delta", {"text": event.delta.thinking})
            self._check_cancel()
            response = stream.get_final_message()

        for block in response.content:
            if block.type == "thinking" and block.thinking:
                self.emit("thinking", {"text": block.thinking})
            elif block.type == "text" and block.text.strip():
                self.emit("message", {"text": block.text})
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.emit(
                "usage",
                {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            )
        return response

    def _run_tool(self, block) -> dict[str, Any]:
        tool_input = block.input if isinstance(block.input, dict) else {}
        self.emit("tool_call", {"id": block.id, "name": block.name, "input": tool_input})
        is_error = False
        try:
            output = self.executor.execute(block.name, tool_input)
        except ToolError as exc:
            output, is_error = f"Error: {exc}", True
        except Exception as exc:  # a tool crash should not kill the whole task
            output, is_error = f"Error: {type(exc).__name__}: {exc}", True
        self.emit("tool_result", {"id": block.id, "output": output, "is_error": is_error})
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
        if is_error:
            result["is_error"] = True
        return result


def _text_of(content) -> str:
    return "\n\n".join(b.text for b in content if b.type == "text").strip()
