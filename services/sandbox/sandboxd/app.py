"""FastAPI application of the sandbox agent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

from . import __version__
from .bootstrap import bootstrap
from .config import Settings
from .routers import as2, chat, experiments, files, ide, tools, workspace
from .security import AgentAuth
from .supervisor import ManagedProcess
from .workspace_env import child_env

log = logging.getLogger("sandboxd")


class Activity:
    """Last time a user-visible action happened; the control plane uses it for idle stop."""

    def __init__(self) -> None:
        self.last = time.time()

    def touch(self) -> None:
        self.last = time.time()


def _probe_versions(settings: Settings) -> dict:
    versions = {"sandboxd": __version__}
    probes = {
        "agentsociety2": [settings.python, "-c", "import importlib.metadata as m; print(m.version('agentsociety2'))"],
        "claude_code": [settings.claude_bin, "--version"],
    }
    if settings.ide_enabled:
        probes["code_server"] = [settings.code_server_bin, "--version"]
    for name, argv in probes.items():
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=child_env(settings))
            versions[name] = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired):
            versions[name] = None
    return versions


def _backend_process(settings: Settings) -> ManagedProcess:
    return ManagedProcess(
        "agentsociety2-backend",
        [settings.python, "-m", "agentsociety2.backend.run", "--log-level", "info"],
        cwd=settings.workspace,
        env_factory=lambda: child_env(settings, extra={"WORKSPACE_PATH": str(settings.workspace)}),
        log_path=settings.logs_dir / "backend.log",
        health_url=f"http://127.0.0.1:{settings.backend_port}/health",
    )


def _ide_process(settings: Settings) -> ManagedProcess:
    data = settings.state_dir / "code-server"
    return ManagedProcess(
        "code-server",
        [
            settings.code_server_bin,
            "--bind-addr", f"127.0.0.1:{settings.ide_port}",
            "--auth", "none",
            "--disable-telemetry",
            "--disable-update-check",
            "--disable-workspace-trust",
            "--user-data-dir", str(data / "user-data"),
            "--extensions-dir", str(data / "extensions"),
            str(settings.workspace),
        ],
        cwd=settings.workspace,
        env_factory=lambda: child_env(settings),
        log_path=settings.logs_dir / "code-server.log",
        health_url=f"http://127.0.0.1:{settings.ide_port}/healthz",
    )


def create_app(settings: Settings | None = None, *, run_bootstrap: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.bootstrap = None
        if run_bootstrap:
            try:
                app.state.bootstrap = (await asyncio.to_thread(bootstrap, settings)).as_dict()
            except Exception as exc:  # report through /status instead of crash-looping
                log.exception("bootstrap failed")
                app.state.bootstrap = {"error": str(exc)}
        app.state.versions = await asyncio.to_thread(_probe_versions, settings)
        if settings.enable_backend:
            app.state.backend = _backend_process(settings)
            await app.state.backend.start()
        if settings.ide_enabled:
            app.state.ide = _ide_process(settings)
            await app.state.ide.start()
        try:
            yield
        finally:
            await app.state.chat.shutdown()
            for proc in (app.state.backend, app.state.ide):
                if proc is not None:
                    with contextlib.suppress(Exception):
                        await proc.stop()
            await app.state.backend_client.aclose()
            await app.state.ide_client.aclose()

    app = FastAPI(title="sandboxd", version=__version__, lifespan=lifespan, docs_url="/docs", openapi_url="/openapi.json")
    app.state.settings = settings
    app.state.activity = Activity()
    app.state.backend = None
    app.state.ide = None
    app.state.versions = {"sandboxd": __version__}
    app.state.jobs = tools.JobManager(settings)
    app.state.chat = chat.ChatManager(settings)
    app.state.backend_client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.backend_port}", timeout=httpx.Timeout(300, connect=5)
    )
    app.state.ide_client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.ide_port}", timeout=httpx.Timeout(120, connect=5)
    )

    for module in (files, experiments, tools, workspace, chat, as2, ide):
        app.include_router(module.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status", dependencies=[AgentAuth])
    async def status(request: Request):
        running = await asyncio.to_thread(experiments.running_experiments, settings.workspace)
        active_chats = app.state.chat.active_turns()
        active_jobs = len(app.state.jobs.active())
        return {
            "project_id": settings.project_id,
            "busy": bool(running or active_chats or active_jobs),
            "running_experiments": running,
            "active_chat_turns": active_chats,
            "active_jobs": active_jobs,
            "last_activity": app.state.activity.last,
            "backend": app.state.backend.status() if app.state.backend else None,
            "ide": app.state.ide.status() if app.state.ide else None,
            "bootstrap": app.state.bootstrap,
            "versions": app.state.versions,
        }

    @app.post("/backend/restart", dependencies=[AgentAuth])
    async def restart_backend():
        if app.state.backend is None:
            return {"restarted": False}
        await app.state.backend.restart()
        healthy = await app.state.backend.wait_healthy(60)
        return {"restarted": True, "healthy": healthy}

    @app.get("/backend/logs", dependencies=[AgentAuth])
    async def backend_logs():
        return {"content": app.state.backend.tail_log() if app.state.backend else ""}

    return app
