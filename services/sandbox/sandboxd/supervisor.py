"""Supervision of long-lived child processes (the agentsociety2 backend, code-server)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger("sandboxd.supervisor")


def terminate_group(pid: int, *, grace: float = 10.0) -> None:
    """SIGTERM a process group, then SIGKILL it if it outlives ``grace`` seconds."""
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


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as dead.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        with contextlib.suppress(OSError, IndexError):
            if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    return True


class ManagedProcess:
    """Keeps one child process running, restarting it with exponential backoff."""

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path,
        env_factory: Callable[[], dict[str, str]],
        log_path: Path,
        health_url: str | None = None,
        max_backoff: float = 60.0,
    ) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.env_factory = env_factory
        self.log_path = log_path
        self.health_url = health_url
        self.max_backoff = max_backoff
        self.process: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.started_at: float | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "pid": self.process.pid if self.running and self.process else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "uptime_s": round(time.monotonic() - self.started_at, 1) if self.running and self.started_at else None,
        }

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run_forever(), name=f"supervise-{self.name}")

    async def _spawn(self) -> asyncio.subprocess.Process:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(self.log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child
        try:
            return await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd),
                env=self.env_factory(),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                self.process = await self._spawn()
            except OSError as exc:
                log.error("%s failed to start: %s", self.name, exc)
                self.last_exit_code = -1
            else:
                self.started_at = time.monotonic()
                log.info("%s started (pid %s)", self.name, self.process.pid)
                code = await self.process.wait()
                self.last_exit_code = code
                if self._stopping:
                    break
                uptime = time.monotonic() - self.started_at
                log.warning("%s exited with %s after %.1fs", self.name, code, uptime)
                if uptime > 120:
                    backoff = 1.0
            self.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def stop(self) -> None:
        self._stopping = True
        if self.process is not None and self.process.returncode is None:
            await asyncio.to_thread(terminate_group, self.process.pid, grace=5.0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def wait_healthy(self, timeout: float = 60.0) -> bool:
        if not self.health_url:
            return self.running
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.process is not None and self.process.returncode is not None and self._stopping:
                    return False
                with contextlib.suppress(httpx.HTTPError):
                    resp = await client.get(self.health_url)
                    if resp.status_code < 500:
                        return True
                await asyncio.sleep(0.5)
        return False

    def tail_log(self, max_bytes: int = 64 * 1024) -> str:
        if not self.log_path.exists():
            return ""
        size = self.log_path.stat().st_size
        with open(self.log_path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
