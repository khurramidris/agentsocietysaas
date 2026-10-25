"""Shared fixtures for the control-plane tests.

* ``cp`` - the app in-process (ASGI transport, lifespan running) with an
  :class:`InProcessDriver` whose "sandboxes" are real sandboxd apps served
  in-process, so proxying and sandbox auth are exercised for real.
* ``live_cp`` - the same app on a real port (uvicorn in a thread), for the
  unmodified upstream clients (dataset skills, literature MCP client).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn

from controlplane import migrate
from controlplane.api import auth as auth_api
from controlplane.app import create_app
from controlplane.sandboxes.base import SandboxDriver, SandboxInfo, SandboxSpec
from controlplane.settings import Settings

REPO = Path(__file__).resolve().parents[3]
PASSWORD = "password-123"


# ----------------------------------------------------------------- settings/db


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CP_"):
            monkeypatch.delenv(key)
    auth_api._attempts.clear()


@pytest.fixture(scope="session")
def template_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("template") / "cp.db"
    migrate.upgrade(f"sqlite+aiosqlite:///{path}")
    return path


def make_settings(tmp_path: Path, template_db: Path, **overrides: Any) -> Settings:
    db = tmp_path / "cp.db"
    if not db.exists():
        shutil.copy(template_db, db)
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{db}",
        "auto_migrate": False,
        "data_dir": tmp_path / "data",
        "secret_key": "test-secret-key-0123456789abcdef0123456789",
        "enable_mock_provider": True,
        "public_base_url": "http://testserver",
        "internal_base_url": "http://testserver",
        "usage_flush_interval_s": 3600,  # tests flush explicitly
        "sandbox_reaper_interval_s": 3600,
        "sandbox_enable_ide": True,
        "signup_credit_usd": 5.0,
        "default_sim_model": "mock",
        "default_chat_model": "mock",
        "default_chat_small_model": "mock",
        "default_embedding_model": "mock-embedding",
        "literature_sources": ["local"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------- in-process driver


class InProcessDriver(SandboxDriver):
    """Sandboxes are real sandboxd apps hosted in this process (no bootstrap/backend)."""

    name = "inprocess"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.apps: dict[str, Any] = {}  # host -> sandboxd app
        self.hosts: dict[str, str] = {}  # project id -> host
        self.specs: list[SandboxSpec] = []
        self.fail_next_start: Exception | None = None

    def workspace(self, project_id: str) -> Path:
        return self.root / project_id / "workspace"

    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        if self.fail_next_start is not None:
            exc, self.fail_next_start = self.fail_next_start, None
            raise exc
        from sandboxd.app import create_app as create_sandbox_app
        from sandboxd.config import Settings as SandboxSettings

        host = f"{spec.name}.sandbox.test"
        workspace = self.workspace(spec.project_id)
        state = self.root / spec.project_id / "state"
        workspace.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        settings = SandboxSettings(
            workspace=workspace,
            state_dir=state,
            agent_secret=spec.env["SANDBOX_AGENT_SECRET"],
            project_id=spec.project_id,
            enable_backend=False,
            ide_enabled=False,
        )
        self.apps[host] = create_sandbox_app(settings, run_bootstrap=False)
        self.hosts[spec.project_id] = host
        self.specs.append(spec)
        return SandboxInfo(state="running", endpoint=f"http://{host}", ref=host)

    def kill(self, project_id: str) -> None:
        """Simulate the sandbox disappearing behind the control plane's back."""
        host = self.hosts.pop(project_id, None)
        self.apps.pop(host, None)

    async def stop(self, project_id: str) -> None:
        self.kill(project_id)

    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        self.kill(project_id)
        if delete_data:
            shutil.rmtree(self.root / project_id, ignore_errors=True)

    async def inspect(self, project_id: str) -> SandboxInfo:
        host = self.hosts.get(project_id)
        if host is None:
            return SandboxInfo(state="stopped")
        return SandboxInfo(state="running", endpoint=f"http://{host}", ref=host)


class HostDispatch:
    """ASGI app routing requests to the sandbox app named by the Host header."""

    def __init__(self, driver: InProcessDriver) -> None:
        self.driver = driver

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode().split(":")[0]
        app = self.driver.apps.get(host)
        if app is None:
            raise httpx.ConnectError(f"no sandbox at {host}")
        await app(scope, receive, send)


# ---------------------------------------------------------------- clients


@dataclass
class Account:
    email: str
    token: str
    user_id: str
    org_id: str

    @property
    def h(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}


@dataclass
class CP:
    app: Any
    client: httpx.AsyncClient
    driver: InProcessDriver
    settings: Settings
    accounts: list[Account] = field(default_factory=list)

    @property
    def ctx(self):
        return self.app.state.ctx

    async def signup(self, email: str, *, name: str = "", org_name: str | None = None) -> Account:
        body: dict[str, Any] = {"email": email, "password": PASSWORD, "name": name}
        if org_name:
            body["org_name"] = org_name
        r = await self.client.post("/api/v1/auth/signup", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        account = Account(email=email, token=data["access_token"], user_id=data["user"]["id"], org_id=data["orgs"][0]["id"])
        self.accounts.append(account)
        return account

    async def login(self, email: str, password: str = PASSWORD) -> httpx.Response:
        return await self.client.post("/api/v1/auth/login", json={"email": email, "password": password})

    async def add_member(self, owner: Account, member: Account, role: str) -> None:
        r = await self.client.post(f"/api/v1/orgs/{owner.org_id}/invitations", json={"email": member.email, "role": role}, headers=owner.h)
        assert r.status_code == 200, r.text
        r = await self.client.post("/api/v1/auth/invitations/accept", json={"token": r.json()["token"]}, headers=member.h)
        assert r.status_code == 200, r.text

    async def create_project(self, account: Account, name: str = "Echo chambers", *, start: bool = True, org_id: str | None = None, **extra) -> dict:
        r = await self.client.post(f"/api/v1/orgs/{org_id or account.org_id}/projects", json={"name": name, "start": start, **extra}, headers=account.h)
        assert r.status_code == 200, r.text
        project = r.json()
        if start:
            await self.ctx.sandboxes.wait_until_ready(project["id"], 10)
        return project

    async def api_key(self, account: Account, role: str = "member", org_id: str | None = None) -> str:
        r = await self.client.post(f"/api/v1/orgs/{org_id or account.org_id}/api-keys", json={"name": "test", "role": role}, headers=account.h)
        assert r.status_code == 200, r.text
        return r.json()["secret"]

    def project_token(self, project_id: str) -> str:
        """The platform token handed to the project's current sandbox."""
        spec = next(s for s in reversed(self.driver.specs) if s.project_id == project_id)
        return spec.env["PLATFORM_TOKEN"]

    async def flush_usage(self) -> int:
        return await self.ctx.usage.flush()


@pytest.fixture
def driver(tmp_path) -> InProcessDriver:
    return InProcessDriver(tmp_path / "sandboxes")


class Lifespan:
    """Run an app's lifespan in its own task: anyio task groups inside it (the MCP
    session manager) must be exited by the task that entered them, while fixture
    setup and teardown may run in different tasks."""

    def __init__(self, app) -> None:
        self.app = app
        self._started = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        async with self.app.router.lifespan_context(self.app):
            self._started.set()
            await self._stop.wait()

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        started = asyncio.create_task(self._started.wait())
        await asyncio.wait({self._task, started}, return_when=asyncio.FIRST_COMPLETED)
        if self._task.done():
            started.cancel()
            self._task.result()  # re-raise the startup failure
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        await self._task


@pytest_asyncio.fixture
async def cp(tmp_path, template_db, driver, request) -> CP:
    overrides = getattr(request, "param", None) or {}
    settings = make_settings(tmp_path, template_db, **overrides)
    app = create_app(settings, sandbox_driver=driver)
    manager = app.state.ctx.sandboxes
    await manager.http.aclose()
    manager.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=HostDispatch(driver)), timeout=10)
    async with Lifespan(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield CP(app=app, client=client, driver=driver, settings=settings)


# ------------------------------------------------------------------ live server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Live:
    base_url: str
    app: Any
    settings: Settings
    loop: asyncio.AbstractEventLoop | None = None

    def run(self, coro):
        """Run a coroutine on the server's event loop (for direct DB access)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)


@pytest.fixture
def live_cp(tmp_path, template_db) -> Live:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    settings = make_settings(tmp_path, template_db, public_base_url=base, internal_base_url=base)
    app = create_app(settings, sandbox_driver=InProcessDriver(tmp_path / "sandboxes"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    live = Live(base_url=base, app=app, settings=settings)

    def serve() -> None:
        loop = asyncio.new_event_loop()
        live.loop = loop
        loop.run_until_complete(server.serve())
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("live control plane did not start")
    yield live
    server.should_exit = True
    thread.join(15)


def live_signup(live: Live, email: str) -> Account:
    r = httpx.post(f"{live.base_url}/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    data = r.json()
    return Account(email=email, token=data["access_token"], user_id=data["user"]["id"], org_id=data["orgs"][0]["id"])


def live_api_key(live: Live, account: Account, role: str = "member") -> str:
    r = httpx.post(f"{live.base_url}/api/v1/orgs/{account.org_id}/api-keys", json={"name": "cli", "role": role}, headers=account.h)
    assert r.status_code == 200, r.text
    return r.json()["secret"]
