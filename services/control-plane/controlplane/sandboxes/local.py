"""Process-based sandboxes for local development.

Each project runs ``python -m sandboxd`` as a child process of the control plane,
with its workspace under ``<data_dir>/sandboxes/<project_id>``. There is **no
isolation** between projects or from the host, so this driver refuses to run in
production (see Settings.validate_for_production).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..settings import Settings
from .base import SandboxDriver, SandboxInfo, SandboxSpec


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


class LocalProcessDriver(SandboxDriver):
    name = "local"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.data_dir).resolve() / "sandboxes"
        self.root.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self._lock = asyncio.Lock()

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def _ports_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "ports.json"

    def _allocate_ports(self, project_id: str) -> dict[str, int]:
        path = self._ports_path(project_id)
        if path.exists():
            ports = json.loads(path.read_text())
            if all(_port_free(p) for p in ports.values()):
                return ports
        used: set[int] = set()
        for other in self.root.glob("*/ports.json"):
            with contextlib.suppress(Exception):
                used.update(json.loads(other.read_text()).values())
        lo, hi = self.settings.local_sandbox_port_range
        found: list[int] = []
        for port in range(lo, hi):
            if port not in used and _port_free(port):
                found.append(port)
                if len(found) == 3:
                    break
        if len(found) < 3:
            raise RuntimeError("No free ports for local sandbox")
        ports = {"agent": found[0], "backend": found[1], "ide": found[2]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ports))
        return ports

    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        async with self._lock:
            await self._stop_locked(spec.project_id)
            base = self.project_dir(spec.project_id)
            ports = self._allocate_ports(spec.project_id)
            python = self.settings.local_sandbox_python or sys.executable
            env = {**_inheritable_env(), **spec.env}
            env.update(
                {
                    "SANDBOX_HOST": "127.0.0.1",
                    "SANDBOX_PORT": str(ports["agent"]),
                    "SANDBOX_BACKEND_PORT": str(ports["backend"]),
                    "SANDBOX_IDE_PORT": str(ports["ide"]),
                    "SANDBOX_WORKSPACE": str(base / "workspace"),
                    "SANDBOX_STATE_DIR": str(base / "state"),
                    "SANDBOX_ASSETS_DIR": str(Path(self.settings.local_sandbox_assets_dir).resolve()),
                    "SANDBOX_PYTHON": python,
                }
            )
            (base / "workspace").mkdir(parents=True, exist_ok=True)
            log = open(base / "sandboxd.log", "ab")  # noqa: SIM115 - owned by the child
            try:
                proc = subprocess.Popen(  # noqa: S603
                    [python, "-m", "sandboxd"],
                    env=env,
                    cwd=str(base),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log.close()
            self.procs[spec.project_id] = proc
            (base / "pid").write_text(str(proc.pid))
            return SandboxInfo(state="running", endpoint=f"http://127.0.0.1:{ports['agent']}", ref=str(proc.pid))

    async def _stop_locked(self, project_id: str) -> None:
        proc = self.procs.pop(project_id, None)
        pid = proc.pid if proc else None
        pid_file = self.project_dir(project_id) / "pid"
        if pid is None:
            pid = _read_pid(pid_file)
            if pid is not None and not _is_sandboxd(pid):
                pid = None  # stale pid file; the number may belong to an unrelated process now
        if pid:
            await asyncio.to_thread(_terminate_group, pid)
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.wait(timeout=1)
        pid_file.unlink(missing_ok=True)

    async def stop(self, project_id: str) -> None:
        async with self._lock:
            await self._stop_locked(project_id)

    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        await self.stop(project_id)
        if delete_data:
            shutil.rmtree(self.project_dir(project_id), ignore_errors=True)

    async def inspect(self, project_id: str) -> SandboxInfo:
        proc = self.procs.get(project_id)
        ports_path = self._ports_path(project_id)
        endpoint = None
        if ports_path.exists():
            endpoint = f"http://127.0.0.1:{json.loads(ports_path.read_text())['agent']}"
        if proc is not None and proc.poll() is None:
            return SandboxInfo(state="running", endpoint=endpoint, ref=str(proc.pid))
        if proc is not None:
            return SandboxInfo(state="error", endpoint=None, detail=f"sandboxd exited with {proc.returncode}")
        # Started by a previous control-plane process (children run in their own session).
        pid = _read_pid(self.project_dir(project_id) / "pid")
        if pid is not None and _is_sandboxd(pid):
            return SandboxInfo(state="running", endpoint=endpoint, ref=str(pid))
        if not self.project_dir(project_id).exists():
            return SandboxInfo(state="missing")
        return SandboxInfo(state="stopped")

    async def close(self) -> None:
        # Like containers, sandboxes outlive a control-plane restart: the next process finds
        # them through their pid files, and the reaper stops them once idle.
        self.procs.clear()

    async def stop_all(self) -> None:
        for pid_file in self.root.glob("*/pid"):
            await self.stop(pid_file.parent.name)


_SECRET_MARKERS = ("SECRET", "API_KEY", "TOKEN", "PASSWORD", "PRIVATE")


_DROP_PREFIXES = ("CP_", "CLAUDE", "ANTHROPIC_", "CCR_", "OPENAI_")


def _inheritable_env() -> dict[str, str]:
    """The control plane's environment minus its own configuration, LLM tool config and secrets."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_DROP_PREFIXES) and not any(marker in k.upper() for marker in _SECRET_MARKERS)
    }


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _is_sandboxd(pid: int) -> bool:
    """Whether ``pid`` is alive and is a sandboxd process (guards against pid reuse)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True  # no /proc (macOS): trust the pid file
    return b"sandboxd" in cmdline


def _terminate_group(pid: int, grace: float = 10.0) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
