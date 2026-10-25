"""Sandbox driver interface.

A driver creates, starts, stops and destroys the isolated runtime that hosts one
project's workspace (a container, a pod, or - for local development only - a
plain process). Everything above the driver (tokens, health checks, billing,
idle stop) lives in :mod:`controlplane.sandboxes.manager`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Literal

SandboxState = Literal["running", "stopped", "missing", "error"]

AGENT_PORT = 7000


@dataclass
class SandboxSpec:
    project_id: str
    image: str
    env: dict[str, str]
    cpus: float = 2
    memory_gb: float = 4
    ide_enabled: bool = False
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"sbx-{self.project_id.replace('_', '-').lower()}"

    @property
    def volume(self) -> str:
        return f"sbxdata-{self.project_id.replace('_', '-').lower()}"


@dataclass
class SandboxInfo:
    state: SandboxState
    endpoint: str | None = None
    ref: str | None = None
    detail: str = ""


class SandboxDriver(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        """Create (or re-create) and start the sandbox. Persistent data must survive."""

    @abc.abstractmethod
    async def stop(self, project_id: str) -> None:
        """Stop the runtime; keep the project's data."""

    @abc.abstractmethod
    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        """Remove the runtime and, if requested, the project's data."""

    @abc.abstractmethod
    async def inspect(self, project_id: str) -> SandboxInfo:
        """Current state as seen by the infrastructure."""

    async def close(self) -> None:  # pragma: no cover - optional hook
        return None
