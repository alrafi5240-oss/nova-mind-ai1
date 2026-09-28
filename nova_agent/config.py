"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_turns: int = 80
    max_tokens: int = 64000
    command_timeout: int = 300
    sandbox: str = "docker"  # "docker" (isolated) or "local" (dev only)
    docker_image: str = "python:3.11"
    docker_network: str = "bridge"
    docker_memory: str = "4g"
    docker_cpus: str = "2"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    max_concurrent: int = 3
    api_token: str | None = None

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            model=os.environ.get("NOVA_MODEL", cls.model),
            effort=os.environ.get("NOVA_EFFORT", cls.effort),
            max_turns=_int("NOVA_MAX_TURNS", cls.max_turns),
            max_tokens=_int("NOVA_MAX_TOKENS", cls.max_tokens),
            command_timeout=_int("NOVA_COMMAND_TIMEOUT", cls.command_timeout),
            sandbox=os.environ.get("NOVA_SANDBOX", cls.sandbox),
            docker_image=os.environ.get("NOVA_DOCKER_IMAGE", cls.docker_image),
            docker_network=os.environ.get("NOVA_DOCKER_NETWORK", cls.docker_network),
            docker_memory=os.environ.get("NOVA_DOCKER_MEMORY", cls.docker_memory),
            docker_cpus=os.environ.get("NOVA_DOCKER_CPUS", cls.docker_cpus),
            data_dir=Path(os.environ.get("NOVA_DATA_DIR", "data")).resolve(),
            max_concurrent=_int("NOVA_MAX_CONCURRENT", cls.max_concurrent),
            api_token=os.environ.get("NOVA_AGENT_TOKEN") or None,
        )
