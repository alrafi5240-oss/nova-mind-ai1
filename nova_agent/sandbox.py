"""Execution sandboxes: where the agent's shell commands actually run.

Both sandboxes keep one persistent bash process per task so that `cd`,
exported variables and activated virtualenvs survive between commands, the
same way a developer's terminal does.

- DockerSandbox: a throwaway container per task, workspace bind-mounted at
  /workspace, with CPU/memory limits. This is the default and the only mode
  that should face untrusted prompts.
- LocalSandbox: runs bash directly on the host inside the workspace folder.
  For development and tests only - it provides no isolation.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Config

MAX_OUTPUT_CHARS = 30_000


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n\n... [{omitted} characters omitted] ...\n\n{text[-half:]}"


class CommandTimeout(Exception):
    pass


class PersistentShell:
    """A long-lived bash process driven over stdin/stdout with sentinel markers."""

    def __init__(self, argv: list[str], cwd: Path | None = None, env: dict | None = None):
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._cwd,
            env=self._env,
            text=True,
            bufsize=1,
            errors="replace",
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: "queue.Queue[str | None]") -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def run(self, command: str, timeout: float) -> tuple[int, str]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            assert self._proc and self._proc.stdin
            marker = f"__NOVA_DONE_{uuid.uuid4().hex}__"
            # stdin is redirected so a command that reads input cannot swallow
            # the sentinel line that follows it.
            script = f"{{\n{command}\n}} < /dev/null\necho \"{marker}$?\"\n"
            try:
                self._proc.stdin.write(script)
                self._proc.stdin.flush()
            except BrokenPipeError:
                self._proc = None
                return 1, "Error: shell exited unexpectedly; it has been restarted."

            out: list[str] = []
            size = 0
            while True:
                try:
                    line = self._lines.get(timeout=timeout)
                except queue.Empty:
                    self._kill()
                    partial = truncate("".join(out))
                    raise CommandTimeout(
                        f"Command timed out after {timeout:.0f}s; the shell was restarted "
                        f"(working directory and environment were reset).\n{partial}"
                    )
                if line is None:
                    # The command ended the shell itself (e.g. `exit 3`); the next
                    # command starts a fresh one.
                    code = self._proc.wait()
                    self._proc = None
                    return code, truncate("".join(out)) + "[shell exited; a new shell will start]"
                if marker in line:
                    before, _, code = line.partition(marker)
                    if before:
                        out.append(before)
                    try:
                        exit_code = int(code.strip())
                    except ValueError:
                        exit_code = 1
                    return exit_code, truncate("".join(out))
                # Keep memory bounded for very chatty commands.
                if size < MAX_OUTPUT_CHARS * 4:
                    out.append(line)
                    size += len(line)

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._proc = None

    def restart(self) -> None:
        with self._lock:
            self._kill()

    def close(self) -> None:
        self.restart()


class Sandbox(ABC):
    """Runs shell commands against a task's workspace."""

    def __init__(self, root: Path, timeout: int):
        self.root = root.resolve()
        self.timeout = timeout

    @property
    @abstractmethod
    def workdir(self) -> str:
        """The workspace path as the agent sees it inside the sandbox."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One line for the system prompt describing the environment."""

    @abstractmethod
    def _shell(self) -> PersistentShell: ...

    def run(self, command: str) -> tuple[int, str]:
        try:
            return self._shell().run(command, self.timeout)
        except CommandTimeout as exc:
            return 124, str(exc)

    def restart(self) -> None:
        self._shell().restart()

    def close(self) -> None:
        self._shell().close()


class LocalSandbox(Sandbox):
    def __init__(self, root: Path, timeout: int):
        super().__init__(root, timeout)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        # Never hand the agent the server's own API credentials.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "NOVA_AGENT_TOKEN"):
            env.pop(key, None)
        self._sh = PersistentShell(["bash", "--noprofile", "--norc"], cwd=self.root, env=env)

    @property
    def workdir(self) -> str:
        return str(self.root)

    @property
    def description(self) -> str:
        return "a bash shell on the host machine (no container isolation)"

    def _shell(self) -> PersistentShell:
        return self._sh


class DockerSandbox(Sandbox):
    WORKDIR = "/workspace"

    def __init__(self, root: Path, timeout: int, config: Config, name: str):
        super().__init__(root, timeout)
        if not shutil.which("docker"):
            raise RuntimeError("NOVA_SANDBOX=docker but the docker CLI is not installed")
        self.container = f"nova-task-{name}"
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0"
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", self.container,
                "--user", user,
                "-e", "HOME=/tmp",
                "-e", "GIT_TERMINAL_PROMPT=0",
                "--memory", config.docker_memory,
                "--cpus", config.docker_cpus,
                "--pids-limit", "512",
                "--network", config.docker_network,
                "-v", f"{self.root}:{self.WORKDIR}",
                "-w", self.WORKDIR,
                config.docker_image,
                "sleep", "infinity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._sh = PersistentShell(
            ["docker", "exec", "-i", "-w", self.WORKDIR, self.container, "bash", "--noprofile", "--norc"]
        )

    @property
    def workdir(self) -> str:
        return self.WORKDIR

    @property
    def description(self) -> str:
        return "a bash shell inside an isolated Docker container"

    def _shell(self) -> PersistentShell:
        return self._sh

    def close(self) -> None:
        super().close()
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)


def make_sandbox(config: Config, root: Path, name: str) -> Sandbox:
    if config.sandbox == "local":
        return LocalSandbox(root, config.command_timeout)
    if config.sandbox == "docker":
        return DockerSandbox(root, config.command_timeout, config, name)
    raise ValueError(f"Unknown NOVA_SANDBOX value: {config.sandbox!r} (use 'docker' or 'local')")
