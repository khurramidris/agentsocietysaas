"""Sandbox lifecycle on top of a driver: tokens, health, idle stop, compute billing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import SANDBOX_SIZES, charge_usage, plan_for, usd_to_micros
from ..db import as_utc, utcnow
from ..models import Organization, Project, ProjectToken, UsageEvent
from ..security import PROJECT_TOKEN_PREFIX, new_composite, sha256_hex
from .base import SandboxDriver, SandboxSpec

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("controlplane.sandboxes")

# Allow a small overdraft so a run in flight is not killed at exactly zero.
OVERDRAFT_MICROS = 1_000_000


def make_driver(ctx: AppContext) -> SandboxDriver:
    kind = ctx.settings.sandbox_driver
    if kind == "docker":
        from .docker import DockerDriver

        return DockerDriver(ctx.settings)
    if kind == "kubernetes":
        from .kubernetes import KubernetesDriver

        return KubernetesDriver(ctx.settings)
    if not ctx.settings.allow_insecure_local_sandboxes and ctx.settings.is_production:
        raise RuntimeError("The local sandbox driver is not allowed in production")
    from .local import LocalProcessDriver

    return LocalProcessDriver(ctx.settings)


class SandboxManager:
    def __init__(self, ctx: AppContext, driver: SandboxDriver | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.driver = driver or make_driver(ctx)
        self._locks: dict[str, asyncio.Lock] = {}
        self._starting: dict[str, asyncio.Task] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))
        self._reaper: asyncio.Task | None = None

    def _lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    # ------------------------------------------------------------------ secrets

    def agent_secret(self, project: Project) -> str:
        return self.ctx.encryptor.decrypt(project.sandbox_secret_enc)

    def agent_headers(self, project: Project) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.agent_secret(project)}"}

    async def issue_project_token(self, db: AsyncSession, project: Project) -> str:
        """Revoke previous sandbox tokens and mint a new one (rotated on each start)."""
        await db.execute(
            update(ProjectToken)
            .where(ProjectToken.project_id == project.id, ProjectToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        token = new_composite(PROJECT_TOKEN_PREFIX)
        db.add(ProjectToken(id=f"ptk_{token.ident}", project_id=project.id, org_id=project.org_id, secret_hash=sha256_hex(token.secret)))
        return token.value

    # ----------------------------------------------------------------- env/spec

    def build_env(self, project: Project, token: str, secret: str) -> dict[str, str]:
        s = self.settings
        ps = project.settings or {}
        base = s.internal_base_url
        return {
            "SANDBOX_AGENT_SECRET": secret,
            "SANDBOX_PROJECT_ID": project.id,
            "SANDBOX_PROJECT_NAME": project.name,
            "SANDBOX_TOPIC": project.topic or project.name,
            "SANDBOX_ENABLE_IDE": "true" if (ps.get("ide_enabled", s.sandbox_enable_ide)) else "false",
            "SANDBOX_SIM_MODEL": ps.get("sim_model") or s.default_sim_model,
            "SANDBOX_CODER_MODEL": ps.get("coder_model") or s.default_coder_model,
            "SANDBOX_EMBEDDING_MODEL": ps.get("embedding_model") or s.default_embedding_model,
            "SANDBOX_EMBEDDING_DIMS": str(ps.get("embedding_dims") or s.default_embedding_dims),
            "SANDBOX_CHAT_MODEL": ps.get("chat_model") or s.default_chat_model,
            "SANDBOX_CHAT_SMALL_MODEL": ps.get("chat_small_model") or s.default_chat_small_model,
            "PLATFORM_TOKEN": token,
            "PLATFORM_LLM_BASE_URL": f"{base}/llm/v1",
            "PLATFORM_ANTHROPIC_BASE_URL": f"{base}/llm/anthropic",
            "PLATFORM_LITERATURE_MCP_URL": f"{base}/literature/mcp/",
            "PLATFORM_DATASET_SERVER": base,
        }

    # ------------------------------------------------------------- operations

    async def _check_quota(self, db: AsyncSession, project: Project) -> Organization:
        org = await db.get(Organization, project.org_id)
        assert org is not None
        plan = plan_for(org)
        if org.balance_micros <= -OVERDRAFT_MICROS:
            raise HTTPException(402, "Out of credits: add credits to start this sandbox")
        if project.sandbox_size not in plan.sandbox_sizes:
            raise HTTPException(403, f"The {plan.label} plan does not include {project.sandbox_size} sandboxes")
        running = (
            await db.execute(
                select(Project.id).where(
                    Project.org_id == org.id,
                    Project.id != project.id,
                    Project.sandbox_status.in_(("starting", "running")),
                )
            )
        ).all()
        if len(running) >= plan.max_running_sandboxes:
            raise HTTPException(
                409, f"The {plan.label} plan allows {plan.max_running_sandboxes} running sandbox(es); stop one first"
            )
        return org

    async def _is_alive(self, project: Project) -> bool:
        """Whether the sandbox recorded as starting/running still exists in the driver."""
        if project.sandbox_status == "starting":
            task = self._starting.get(project.id)
            if task is not None and not task.done():
                return True
        info = await self.driver.inspect(project.id)
        if info.state != "running":
            return False
        if project.sandbox_status == "starting":
            # The control plane restarted mid-start: resume waiting for readiness.
            self._starting[project.id] = asyncio.create_task(self._await_ready(project.id))
        return True

    async def _mark_dead(self, db: AsyncSession, project: Project) -> None:
        log.warning("sandbox %s is recorded as %s but is gone; marking it stopped", project.id, project.sandbox_status)
        project.sandbox_status = "stopped"
        project.sandbox_endpoint = None
        project.sandbox_error = "sandbox exited unexpectedly"
        await db.execute(
            update(ProjectToken)
            .where(ProjectToken.project_id == project.id, ProjectToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await db.commit()

    async def reconcile_all(self) -> int:
        """Mark sandboxes that no longer exist (e.g. after a restart) as stopped. Returns how many."""
        dead = 0
        async with self.ctx.db.sessionmaker() as db:
            projects = (
                await db.execute(select(Project).where(Project.sandbox_status.in_(("running", "starting", "stopping"))))
            ).scalars().all()
            for project in projects:
                try:
                    async with self._lock(project.id):
                        if project.sandbox_status == "stopping" or not await self._is_alive(project):
                            await self._mark_dead(db, project)
                            dead += 1
                except Exception:
                    log.exception("reconcile failed for %s", project.id)
                    await db.rollback()
        return dead

    async def start(self, db: AsyncSession, project: Project) -> Project:
        async with self._lock(project.id):
            await db.refresh(project)
            if project.sandbox_status in ("starting", "running"):
                if await self._is_alive(project):
                    return project
                await self._mark_dead(db, project)
            await self._check_quota(db, project)
            secret = secrets.token_urlsafe(32)
            token = await self.issue_project_token(db, project)
            size = SANDBOX_SIZES.get(project.sandbox_size, SANDBOX_SIZES["small"])
            spec = SandboxSpec(
                project_id=project.id,
                image=self.settings.sandbox_image,
                env=self.build_env(project, token, secret),
                cpus=size["cpus"],
                memory_gb=size["memory_gb"],
                ide_enabled=(project.settings or {}).get("ide_enabled", self.settings.sandbox_enable_ide),
            )
            project.sandbox_secret_enc = self.ctx.encryptor.encrypt(secret)
            project.sandbox_status = "starting"
            project.sandbox_error = None
            await db.commit()
            try:
                info = await self.driver.start(spec)
            except Exception as exc:
                log.exception("sandbox start failed for %s", project.id)
                project.sandbox_status = "error"
                project.sandbox_error = f"start failed: {exc}"[:2000]
                await db.commit()
                raise HTTPException(500, project.sandbox_error) from exc
            project.sandbox_ref = info.ref
            project.sandbox_endpoint = info.endpoint
            now = utcnow()
            project.sandbox_started_at = now
            project.sandbox_billed_until = now
            project.sandbox_last_activity_at = now
            await db.commit()
        task = asyncio.create_task(self._await_ready(project.id))
        self._starting[project.id] = task
        return project

    async def _await_ready(self, project_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.sandbox_start_timeout_s
        async with self.ctx.db.sessionmaker() as db:
            while asyncio.get_running_loop().time() < deadline:
                project = await db.get(Project, project_id)
                if project is None or project.sandbox_status != "starting":
                    return
                if not project.sandbox_endpoint:
                    info = await self.driver.inspect(project_id)
                    if info.endpoint:
                        project.sandbox_endpoint = info.endpoint
                        await db.commit()
                    if info.state == "error":
                        project.sandbox_status = "error"
                        project.sandbox_error = info.detail
                        await db.commit()
                        return
                if project.sandbox_endpoint:
                    with contextlib.suppress(httpx.HTTPError):
                        resp = await self.http.get(
                            f"{project.sandbox_endpoint}/status", headers=self.agent_headers(project), timeout=5
                        )
                        if resp.status_code == 200:
                            project.sandbox_status = "running"
                            await db.commit()
                            log.info("sandbox %s is ready", project_id)
                            return
                await db.rollback()
                await asyncio.sleep(1)
            project = await db.get(Project, project_id)
            if project is not None and project.sandbox_status == "starting":
                info = await self.driver.inspect(project_id)
                project.sandbox_status = "error"
                project.sandbox_error = f"sandbox did not become ready in {self.settings.sandbox_start_timeout_s}s ({info.detail})"
                await db.commit()

    async def wait_until_ready(self, project_id: str, timeout: float) -> None:
        task = self._starting.get(project_id)
        if task is not None and not task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def stop(self, db: AsyncSession, project: Project, *, reason: str = "user") -> Project:
        async with self._lock(project.id):
            await self._bill_compute(db, project)
            project.sandbox_status = "stopping"
            await db.commit()
            try:
                await self.driver.stop(project.id)
            finally:
                project.sandbox_status = "stopped"
                project.sandbox_endpoint = None
                project.sandbox_error = None if reason == "user" else f"stopped: {reason}"
                await db.execute(
                    update(ProjectToken)
                    .where(ProjectToken.project_id == project.id, ProjectToken.revoked_at.is_(None))
                    .values(revoked_at=utcnow())
                )
                await db.commit()
        log.info("sandbox %s stopped (%s)", project.id, reason)
        return project

    async def destroy(self, db: AsyncSession, project: Project) -> None:
        if project.sandbox_status in ("starting", "running", "stopping"):
            await self.stop(db, project, reason="deleted")
        await self.driver.destroy(project.id, delete_data=True)

    async def status(self, db: AsyncSession, project: Project) -> dict:
        data = {
            "status": project.sandbox_status,
            "size": project.sandbox_size,
            "started_at": as_utc(project.sandbox_started_at),
            "error": project.sandbox_error,
            "driver": self.driver.name,
        }
        if project.sandbox_status == "running" and project.sandbox_endpoint:
            try:
                resp = await self.http.get(f"{project.sandbox_endpoint}/status", headers=self.agent_headers(project), timeout=5)
                if resp.status_code == 200:
                    data["agent"] = resp.json()
            except httpx.HTTPError as exc:
                data["agent_error"] = str(exc)
        return data

    # ------------------------------------------------------------ reconciling

    async def _bill_compute(self, db: AsyncSession, project: Project) -> None:
        """Charge sandbox time since the last billing point."""
        if project.sandbox_status not in ("starting", "running") or project.sandbox_billed_until is None:
            return
        now = utcnow()
        start = as_utc(project.sandbox_billed_until)
        seconds = int((now - start).total_seconds())
        if seconds <= 0:
            return
        hourly = self.settings.sandbox_hourly_usd.get(project.sandbox_size, 0.06)
        cost = usd_to_micros(hourly * seconds / 3600)
        db.add(
            UsageEvent(
                org_id=project.org_id,
                project_id=project.id,
                source="platform",
                kind="compute",
                model=f"sandbox-{project.sandbox_size}",
                quantity=seconds,
                cost_micros=cost,
            )
        )
        await charge_usage(db, project.org_id, cost)
        project.sandbox_billed_until = start + timedelta(seconds=seconds)

    async def reap_once(self) -> dict[str, int]:
        """Bill running sandboxes, stop idle or unfunded ones, mark dead ones."""
        stats = {"billed": 0, "stopped_idle": 0, "stopped_credit": 0, "errors": 0}
        async with self.ctx.db.sessionmaker() as db:
            projects = (
                await db.execute(select(Project).where(Project.sandbox_status.in_(("running", "starting"))))
            ).scalars().all()
            for project in projects:
                try:
                    await self._bill_compute(db, project)
                    stats["billed"] += 1
                    await db.commit()
                    org = await db.get(Organization, project.org_id)
                    agent = None
                    if project.sandbox_status == "running" and project.sandbox_endpoint:
                        with contextlib.suppress(httpx.HTTPError, ValueError):
                            resp = await self.http.get(
                                f"{project.sandbox_endpoint}/status", headers=self.agent_headers(project), timeout=5
                            )
                            agent = resp.json() if resp.status_code == 200 else None
                        if agent is None:
                            info = await self.driver.inspect(project.id)
                            if info.state != "running":
                                project.sandbox_status = "error" if info.state == "error" else "stopped"
                                project.sandbox_error = info.detail or "sandbox exited"
                                await db.commit()
                                stats["errors"] += 1
                                continue
                    busy = bool(agent and agent.get("busy"))
                    last = as_utc(project.sandbox_last_activity_at) or utcnow()
                    if agent and agent.get("last_activity"):
                        last = max(last, datetime.fromtimestamp(float(agent["last_activity"]), UTC))
                    if org is not None and org.balance_micros <= -OVERDRAFT_MICROS:
                        await self.stop(db, project, reason="out of credits")
                        stats["stopped_credit"] += 1
                    elif project.sandbox_status == "running" and not busy and utcnow() - last > self.idle_limit(org):
                        await self.stop(db, project, reason="idle")
                        stats["stopped_idle"] += 1
                except Exception:
                    log.exception("reaper failed for %s", project.id)
                    stats["errors"] += 1
                    await db.rollback()
        return stats

    def idle_limit(self, org: Organization | None) -> timedelta:
        """Plan idle timeout, capped by CP_SANDBOX_IDLE_STOP_S when that is set."""
        limit = timedelta(minutes=plan_for(org).idle_stop_minutes) if org else timedelta(minutes=15)
        if self.settings.sandbox_idle_stop_s:
            limit = min(limit, timedelta(seconds=self.settings.sandbox_idle_stop_s))
        return limit

    async def _reaper_loop(self) -> None:
        with contextlib.suppress(Exception):
            await self.reconcile_all()
        while True:
            await asyncio.sleep(self.settings.sandbox_reaper_interval_s)
            with contextlib.suppress(Exception):
                await self.reap_once()

    def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reaper_loop())

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        await self.http.aclose()
        await self.driver.close()

    async def touch(self, db: AsyncSession, project: Project) -> None:
        now = utcnow()
        last = as_utc(project.sandbox_last_activity_at)
        if last is None or (now - last).total_seconds() > 30:
            project.sandbox_last_activity_at = now
            await db.commit()
