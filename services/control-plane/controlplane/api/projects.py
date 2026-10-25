"""Projects (one research workspace each) and their sandboxes."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..billing import SANDBOX_SIZES, plan_for
from ..db import as_utc, utcnow
from ..deps import Principal, ctx, get_db, get_principal, org_access, project_access
from ..models import Project, UsageEvent

router = APIRouter(tags=["projects"])

SETTING_KEYS = {"sim_model", "coder_model", "embedding_model", "embedding_dims", "chat_model", "chat_small_model", "ide_enabled"}


def project_out(p: Project) -> dict:
    return {
        "id": p.id,
        "org_id": p.org_id,
        "name": p.name,
        "slug": p.slug,
        "description": p.description,
        "topic": p.topic,
        "settings": p.settings or {},
        "created_at": as_utc(p.created_at),
        "updated_at": as_utc(p.updated_at),
        "sandbox": {
            "status": p.sandbox_status,
            "size": p.sandbox_size,
            "started_at": as_utc(p.sandbox_started_at),
            "last_activity_at": as_utc(p.sandbox_last_activity_at),
            "error": p.sandbox_error,
        },
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "project"


class ProjectSettings(BaseModel):
    sim_model: str | None = Field(None, max_length=160)
    coder_model: str | None = Field(None, max_length=160)
    embedding_model: str | None = Field(None, max_length=160)
    embedding_dims: int | None = Field(None, ge=8, le=8192)
    chat_model: str | None = Field(None, max_length=160)
    chat_small_model: str | None = Field(None, max_length=160)
    ide_enabled: bool | None = None


class CreateProject(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    topic: str = Field("", max_length=20000)
    sandbox_size: str = "small"
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    start: bool = True


@router.get("/api/v1/orgs/{org_id}/projects")
async def list_projects(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    rows = (
        await db.execute(select(Project).where(Project.org_id == org_id, Project.archived_at.is_(None)).order_by(Project.updated_at.desc()))
    ).scalars().all()
    return {"projects": [project_out(p) for p in rows]}


@router.post("/api/v1/orgs/{org_id}/projects")
async def create_project(org_id: str, body: CreateProject, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "member")
    plan = plan_for(access.org)
    count = (await db.execute(select(func.count(Project.id)).where(Project.org_id == org_id, Project.archived_at.is_(None)))).scalar_one()
    if count >= plan.max_projects:
        raise HTTPException(403, f"The {plan.label} plan allows {plan.max_projects} projects")
    if body.sandbox_size not in SANDBOX_SIZES:
        raise HTTPException(400, f"sandbox_size must be one of {list(SANDBOX_SIZES)}")
    if body.sandbox_size not in plan.sandbox_sizes:
        raise HTTPException(403, f"The {plan.label} plan does not include {body.sandbox_size} sandboxes")
    slug = _slug(body.name)
    base = slug
    n = 1
    while (await db.execute(select(Project.id).where(Project.org_id == org_id, Project.slug == slug))).first() is not None:
        n += 1
        slug = f"{base}-{n}"
    project = Project(
        org_id=org_id,
        name=body.name,
        slug=slug,
        description=body.description,
        topic=body.topic or body.name,
        created_by=principal.user_id,
        sandbox_size=body.sandbox_size,
        settings={k: v for k, v in body.settings.model_dump().items() if v is not None},
    )
    db.add(project)
    audit(db, request, "project.created", org_id=org_id, user_id=principal.user_id, target_type="project", target_id=project.id)
    await db.commit()
    if body.start:
        try:
            await ctx(request).sandboxes.start(db, project)
        except HTTPException as exc:
            # The project exists; surface why its sandbox could not start yet.
            project.sandbox_error = str(exc.detail)
            await db.commit()
    await db.refresh(project)
    return project_out(project)


@router.get("/api/v1/projects/{project_id}")
async def get_project(project_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id)
    return {**project_out(pa.project), "role": pa.access.role}


class UpdateProject(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    topic: str | None = Field(None, max_length=20000)
    sandbox_size: str | None = None
    settings: ProjectSettings | None = None


@router.patch("/api/v1/projects/{project_id}")
async def update_project(project_id: str, body: UpdateProject, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id, "member")
    p = pa.project
    if body.name is not None:
        p.name = body.name
    if body.description is not None:
        p.description = body.description
    if body.topic is not None:
        p.topic = body.topic
    if body.sandbox_size is not None:
        plan = plan_for(pa.access.org)
        if body.sandbox_size not in plan.sandbox_sizes:
            raise HTTPException(403, f"The {plan.label} plan does not include {body.sandbox_size} sandboxes")
        p.sandbox_size = body.sandbox_size
    if body.settings is not None:
        merged = dict(p.settings or {})
        for key, value in body.settings.model_dump(exclude_unset=True).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        p.settings = merged
    p.updated_at = utcnow()
    audit(db, request, "project.updated", org_id=p.org_id, user_id=principal.user_id, target_type="project", target_id=p.id)
    await db.commit()
    return {**project_out(p), "restart_required": p.sandbox_status == "running" and (body.settings is not None or body.sandbox_size is not None)}


@router.delete("/api/v1/projects/{project_id}")
async def delete_project(project_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id, "admin")
    await ctx(request).sandboxes.destroy(db, pa.project)
    pa.project.archived_at = utcnow()
    pa.project.sandbox_status = "deleted"
    audit(db, request, "project.deleted", org_id=pa.project.org_id, user_id=principal.user_id, target_type="project", target_id=project_id)
    await db.commit()
    return {"deleted": project_id}


# ------------------------------------------------------------------- sandbox


@router.get("/api/v1/projects/{project_id}/sandbox")
async def sandbox_status(project_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id)
    return await ctx(request).sandboxes.status(db, pa.project)


@router.post("/api/v1/projects/{project_id}/sandbox/start")
async def sandbox_start(project_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id, "member")
    project = await ctx(request).sandboxes.start(db, pa.project)
    audit(db, request, "sandbox.started", org_id=project.org_id, user_id=principal.user_id, target_type="project", target_id=project.id)
    await db.commit()
    return project_out(project)


@router.post("/api/v1/projects/{project_id}/sandbox/stop")
async def sandbox_stop(project_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id, "member")
    if pa.project.sandbox_status in ("stopped", "deleted"):
        return project_out(pa.project)
    project = await ctx(request).sandboxes.stop(db, pa.project, reason="user")
    audit(db, request, "sandbox.stopped", org_id=project.org_id, user_id=principal.user_id, target_type="project", target_id=project.id)
    await db.commit()
    return project_out(project)


@router.post("/api/v1/projects/{project_id}/sandbox/restart")
async def sandbox_restart(project_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id, "member")
    manager = ctx(request).sandboxes
    if pa.project.sandbox_status in ("running", "starting", "error"):
        await manager.stop(db, pa.project, reason="user")
    project = await manager.start(db, pa.project)
    return project_out(project)


@router.get("/api/v1/projects/{project_id}/usage")
async def project_usage(project_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    pa = await project_access(db, principal, project_id)
    rows = (
        await db.execute(
            select(
                UsageEvent.kind,
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.sum(UsageEvent.input_tokens),
                func.sum(UsageEvent.output_tokens),
                func.sum(UsageEvent.cached_tokens),
                func.sum(UsageEvent.quantity),
                func.sum(UsageEvent.cost_micros),
            )
            .where(UsageEvent.project_id == pa.project.id)
            .group_by(UsageEvent.kind, UsageEvent.model)
        )
    ).all()
    breakdown = [
        {
            "kind": kind,
            "model": model,
            "requests": int(n or 0),
            "input_tokens": int(i or 0),
            "output_tokens": int(o or 0),
            "cached_tokens": int(c or 0),
            "seconds": int(q or 0),
            "cost_usd": round(int(cost or 0) / 1_000_000, 6),
        }
        for kind, model, n, i, o, c, q, cost in rows
    ]
    return {"project_id": project_id, "total_cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6), "breakdown": breakdown}
