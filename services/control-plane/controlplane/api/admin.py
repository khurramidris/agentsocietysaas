"""Platform administration (operators of the hosted service)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..billing import PLANS, SANDBOX_SIZES, adjust_balance, usd_to_micros
from ..db import utcnow
from ..deps import Principal, ctx, get_db, require_platform_admin
from ..models import AuditLog, Dataset, Organization, Project, UsageEvent, User
from .orgs import org_out

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])
meta_router = APIRouter(tags=["meta"])


@meta_router.get("/api/v1/meta")
async def meta(request: Request):
    settings = ctx(request).settings
    return {
        "brand_name": settings.brand_name,
        "environment": settings.environment,
        "signup_enabled": settings.allow_signup,
        "features": {
            "stripe": ctx(request).stripe.enabled,
            "ide": settings.sandbox_enable_ide,
            "mock_models": settings.enable_mock_provider,
            "sandbox_driver": settings.sandbox_driver,
        },
        "ide_base_url": settings.effective_ide_base_url,
        "mapbox_token": settings.mapbox_token,
        "defaults": {
            "sim_model": settings.default_sim_model,
            "chat_model": settings.default_chat_model,
            "embedding_model": settings.default_embedding_model,
        },
        "sandbox_sizes": SANDBOX_SIZES,
        "sandbox_hourly_usd": settings.sandbox_hourly_usd,
        "plans": [{"id": p.id, "label": p.label, "price_usd_month": p.price_usd_month} for p in PLANS.values()],
        "upstream": {
            "project": "AgentSociety (Tsinghua FIB Lab)",
            "license": "Apache-2.0",
            "url": "https://github.com/tsinghua-fib-lab/agentsociety",
        },
    }


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    since = utcnow() - timedelta(days=1)
    count = lambda model: select(func.count()).select_from(model)  # noqa: E731
    return {
        "users": (await db.execute(count(User))).scalar_one(),
        "orgs": (await db.execute(count(Organization))).scalar_one(),
        "projects": (await db.execute(count(Project).where(Project.archived_at.is_(None)))).scalar_one(),
        "running_sandboxes": (await db.execute(count(Project).where(Project.sandbox_status.in_(("running", "starting"))))).scalar_one(),
        "usage_24h_usd": round(
            int((await db.execute(select(func.sum(UsageEvent.cost_micros)).where(UsageEvent.created_at >= since))).scalar_one() or 0) / 1e6, 4
        ),
        "requests_24h": (await db.execute(count(UsageEvent).where(UsageEvent.created_at >= since))).scalar_one(),
        "datasets_pending_review": (await db.execute(count(Dataset).where(Dataset.status == "pending_review"))).scalar_one(),
    }


@router.get("/orgs")
async def list_orgs(db: AsyncSession = Depends(get_db), q: str | None = None, limit: int = Query(100, le=1000)):
    stmt = select(Organization).order_by(Organization.created_at.desc()).limit(limit)
    if q:
        stmt = stmt.where(Organization.name.ilike(f"%{q}%") | Organization.slug.ilike(f"%{q}%"))
    return {"orgs": [org_out(o) for o in (await db.execute(stmt)).scalars().all()]}


class GrantBody(BaseModel):
    amount_usd: float = Field(..., ge=-100_000, le=100_000)
    description: str = Field("Manual adjustment", max_length=500)


@router.post("/orgs/{org_id}/credits")
async def grant_credits(
    org_id: str, body: GrantBody, request: Request, principal: Principal = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    if await db.get(Organization, org_id) is None:
        raise HTTPException(404, "Organization not found")
    entry = await adjust_balance(
        db, org_id, usd_to_micros(body.amount_usd), "grant" if body.amount_usd >= 0 else "adjustment", body.description, created_by=principal.user_id
    )
    audit(db, request, "admin.credits_adjusted", org_id=org_id, user_id=principal.user_id, amount_usd=body.amount_usd)
    await db.commit()
    return {"balance_usd": round(entry.balance_after / 1e6, 6) if entry else None}


class PlanBody(BaseModel):
    plan: str


@router.patch("/orgs/{org_id}")
async def set_plan(org_id: str, body: PlanBody, request: Request, principal: Principal = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    if body.plan not in PLANS:
        raise HTTPException(400, f"plan must be one of {list(PLANS)}")
    org.plan = body.plan
    audit(db, request, "admin.plan_changed", org_id=org_id, user_id=principal.user_id, plan=body.plan)
    await db.commit()
    return org_out(org)


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db), q: str | None = None, limit: int = Query(100, le=1000)):
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if q:
        stmt = stmt.where(User.email.ilike(f"%{q}%") | User.name.ilike(f"%{q}%"))
    return {
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "is_active": u.is_active,
                "is_platform_admin": u.is_platform_admin,
                "created_at": u.created_at,
                "last_login_at": u.last_login_at,
            }
            for u in (await db.execute(stmt)).scalars().all()
        ]
    }


class UserPatch(BaseModel):
    is_active: bool | None = None
    is_platform_admin: bool | None = None


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str, body: UserPatch, request: Request, principal: Principal = Depends(require_platform_admin), db: AsyncSession = Depends(get_db)
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == principal.user_id and body.is_platform_admin is False:
        raise HTTPException(409, "You cannot remove your own admin rights")
    for field in ("is_active", "is_platform_admin"):
        value = getattr(body, field)
        if value is not None:
            setattr(user, field, value)
    audit(db, request, "admin.user_updated", user_id=principal.user_id, target_type="user", target_id=user_id, **body.model_dump(exclude_none=True))
    await db.commit()
    return {"id": user.id, "is_active": user.is_active, "is_platform_admin": user.is_platform_admin}


@router.get("/datasets")
async def review_queue(db: AsyncSession = Depends(get_db), status: str = "pending_review"):
    rows = (await db.execute(select(Dataset).where(Dataset.status == status).order_by(Dataset.updated_at.desc()))).scalars().all()
    return {
        "datasets": [
            {
                "id": d.id,
                "org_id": d.org_id,
                "name": d.name,
                "category": d.category,
                "version": d.version,
                "package_size_bytes": d.package_size_bytes,
                "updated_at": d.updated_at,
            }
            for d in rows
        ]
    }


@router.post("/sandboxes/reap")
async def reap(request: Request):
    return await ctx(request).sandboxes.reap_once()


@router.get("/audit")
async def audit_log(db: AsyncSession = Depends(get_db), org_id: str | None = None, limit: int = Query(200, le=2000)):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if org_id:
        stmt = stmt.where(AuditLog.org_id == org_id)
    return {
        "events": [
            {
                "id": e.id,
                "org_id": e.org_id,
                "user_id": e.user_id,
                "action": e.action,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "details": e.details,
                "ip": e.ip,
                "created_at": e.created_at,
            }
            for e in (await db.execute(stmt)).scalars().all()
        ]
    }
