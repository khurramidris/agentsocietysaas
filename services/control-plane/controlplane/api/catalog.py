"""Model catalog management: platform providers/models (admins) and BYOK (org admins).

Organisations can bring their own provider keys ("BYOK"): routes they define are
visible only to them, take precedence over platform routes with the same alias,
and are not charged against platform credits.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..deps import Principal, ctx, get_db, get_principal, org_access, require_platform_admin
from ..models import ModelRoute, Provider

router = APIRouter(tags=["models"])

PROVIDER_KINDS = ("openai", "anthropic", "litellm", "mock")


def provider_out(p: Provider) -> dict:
    return {
        "id": p.id,
        "org_id": p.org_id,
        "name": p.name,
        "kind": p.kind,
        "api_base": p.api_base,
        "has_api_key": bool(p.api_key_enc),
        "enabled": p.enabled,
        "created_at": p.created_at,
    }


def route_out(r: ModelRoute) -> dict:
    per_m = 1000
    return {
        "id": r.id,
        "org_id": r.org_id,
        "alias": r.alias,
        "provider_id": r.provider_id,
        "upstream_model": r.upstream_model,
        "kind": r.kind,
        "description": r.description,
        "enabled": r.enabled,
        "pricing_usd_per_mtok": {
            "input": r.input_nanos / per_m,
            "output": r.output_nanos / per_m,
            "cache_read": r.cache_read_nanos / per_m,
            "cache_write": r.cache_write_nanos / per_m,
            "cache_write_1h": r.cache_write_1h_nanos / per_m,
        },
        "long_context_tokens": r.long_context_tokens,
        "long_context_multiplier": r.long_context_multiplier,
    }


class ProviderBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    kind: str
    api_base: str = Field("", max_length=500)
    api_key: str | None = Field(None, max_length=4000)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class RouteBody(BaseModel):
    alias: str = Field(..., min_length=1, max_length=160)
    provider_id: str
    upstream_model: str = Field(..., min_length=1, max_length=200)
    kind: str = "chat"
    description: str = ""
    input_usd_per_mtok: float = Field(0, ge=0)
    output_usd_per_mtok: float = Field(0, ge=0)
    cache_read_usd_per_mtok: float = Field(0, ge=0)
    cache_write_usd_per_mtok: float = Field(0, ge=0)
    cache_write_1h_usd_per_mtok: float = Field(0, ge=0)
    long_context_tokens: int = Field(0, ge=0)
    long_context_multiplier: float = Field(1.0, ge=1.0)
    enabled: bool = True


def _nanos(usd: float) -> int:
    return int(round(usd * 1000))


async def _create_provider(db: AsyncSession, request: Request, org_id: str | None, body: ProviderBody) -> Provider:
    if body.kind not in PROVIDER_KINDS or (body.kind == "mock" and org_id is not None):
        raise HTTPException(400, f"kind must be one of {PROVIDER_KINDS[:3]}")
    provider = Provider(
        org_id=org_id,
        name=body.name,
        kind=body.kind,
        api_base=body.api_base.rstrip("/"),
        api_key_enc=ctx(request).encryptor.encrypt(body.api_key) if body.api_key else None,
        extra_headers=body.extra_headers,
        enabled=body.enabled,
    )
    db.add(provider)
    await db.commit()
    ctx(request).gateway.invalidate()
    return provider


async def _create_route(db: AsyncSession, request: Request, org_id: str | None, body: RouteBody) -> ModelRoute:
    provider = await db.get(Provider, body.provider_id)
    if provider is None or provider.org_id != org_id:
        raise HTTPException(404, "Provider not found")
    if body.kind not in ("chat", "embedding"):
        raise HTTPException(400, "kind must be chat or embedding")
    owner = ModelRoute.org_id == org_id if org_id else ModelRoute.org_id.is_(None)
    if (await db.execute(select(ModelRoute.id).where(owner, ModelRoute.alias == body.alias))).first():
        raise HTTPException(409, f"Model alias {body.alias!r} already exists")
    billable = org_id is None
    route = ModelRoute(
        org_id=org_id,
        alias=body.alias,
        provider_id=provider.id,
        upstream_model=body.upstream_model,
        kind=body.kind,
        description=body.description,
        input_nanos=_nanos(body.input_usd_per_mtok) if billable else 0,
        output_nanos=_nanos(body.output_usd_per_mtok) if billable else 0,
        cache_read_nanos=_nanos(body.cache_read_usd_per_mtok) if billable else 0,
        cache_write_nanos=_nanos(body.cache_write_usd_per_mtok) if billable else 0,
        cache_write_1h_nanos=_nanos(body.cache_write_1h_usd_per_mtok) if billable else 0,
        long_context_tokens=body.long_context_tokens,
        long_context_multiplier=body.long_context_multiplier,
        enabled=body.enabled,
    )
    db.add(route)
    await db.commit()
    ctx(request).gateway.invalidate()
    return route


# --------------------------------------------------------------- org (BYOK)


@router.get("/api/v1/orgs/{org_id}/models")
async def org_models(org_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    routes = (
        await db.execute(select(ModelRoute).where((ModelRoute.org_id == org_id) | ModelRoute.org_id.is_(None)).order_by(ModelRoute.alias))
    ).scalars().all()
    providers = (await db.execute(select(Provider).where(Provider.org_id == org_id))).scalars().all()
    return {
        "models": [route_out(r) for r in routes if r.enabled or r.org_id == org_id],
        "providers": [provider_out(p) for p in providers],
        "mock_enabled": ctx(request).settings.enable_mock_provider,
    }


@router.post("/api/v1/orgs/{org_id}/providers")
async def org_add_provider(org_id: str, body: ProviderBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    provider = await _create_provider(db, request, org_id, body)
    audit(db, request, "org.provider_added", org_id=org_id, user_id=principal.user_id, target_type="provider", target_id=provider.id, kind=body.kind)
    await db.commit()
    return provider_out(provider)


@router.delete("/api/v1/orgs/{org_id}/providers/{provider_id}")
async def org_delete_provider(org_id: str, provider_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    provider = await db.get(Provider, provider_id)
    if provider is None or provider.org_id != org_id:
        raise HTTPException(404, "Provider not found")
    await db.delete(provider)
    await db.commit()
    ctx(request).gateway.invalidate()
    return {"deleted": provider_id}


@router.post("/api/v1/orgs/{org_id}/models")
async def org_add_model(org_id: str, body: RouteBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    return route_out(await _create_route(db, request, org_id, body))


@router.delete("/api/v1/orgs/{org_id}/models/{route_id}")
async def org_delete_model(org_id: str, route_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    route = await db.get(ModelRoute, route_id)
    if route is None or route.org_id != org_id:
        raise HTTPException(404, "Model not found")
    await db.delete(route)
    await db.commit()
    ctx(request).gateway.invalidate()
    return {"deleted": route_id}


# ------------------------------------------------------------ platform admin

admin = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


@admin.get("/providers")
async def admin_providers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Provider).where(Provider.org_id.is_(None)))).scalars().all()
    return {"providers": [provider_out(p) for p in rows]}


@admin.post("/providers")
async def admin_add_provider(body: ProviderBody, request: Request, db: AsyncSession = Depends(get_db)):
    return provider_out(await _create_provider(db, request, None, body))


class ProviderPatch(BaseModel):
    api_base: str | None = None
    api_key: str | None = None
    enabled: bool | None = None


@admin.patch("/providers/{provider_id}")
async def admin_patch_provider(provider_id: str, body: ProviderPatch, request: Request, db: AsyncSession = Depends(get_db)):
    provider = await db.get(Provider, provider_id)
    if provider is None or provider.org_id is not None:
        raise HTTPException(404, "Provider not found")
    if body.api_base is not None:
        provider.api_base = body.api_base.rstrip("/")
    if body.api_key is not None:
        provider.api_key_enc = ctx(request).encryptor.encrypt(body.api_key) if body.api_key else None
    if body.enabled is not None:
        provider.enabled = body.enabled
    await db.commit()
    ctx(request).gateway.invalidate()
    return provider_out(provider)


@admin.get("/models")
async def admin_models(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ModelRoute).where(ModelRoute.org_id.is_(None)).order_by(ModelRoute.alias))).scalars().all()
    return {"models": [route_out(r) for r in rows]}


@admin.post("/models")
async def admin_add_model(body: RouteBody, request: Request, db: AsyncSession = Depends(get_db)):
    return route_out(await _create_route(db, request, None, body))


class RoutePatch(BaseModel):
    enabled: bool | None = None
    input_usd_per_mtok: float | None = Field(None, ge=0)
    output_usd_per_mtok: float | None = Field(None, ge=0)
    cache_read_usd_per_mtok: float | None = Field(None, ge=0)
    cache_write_usd_per_mtok: float | None = Field(None, ge=0)
    cache_write_1h_usd_per_mtok: float | None = Field(None, ge=0)
    upstream_model: str | None = None
    description: str | None = None


@admin.patch("/models/{route_id}")
async def admin_patch_model(route_id: str, body: RoutePatch, request: Request, db: AsyncSession = Depends(get_db)):
    route = await db.get(ModelRoute, route_id)
    if route is None or route.org_id is not None:
        raise HTTPException(404, "Model not found")
    for field, column in (
        ("input_usd_per_mtok", "input_nanos"),
        ("output_usd_per_mtok", "output_nanos"),
        ("cache_read_usd_per_mtok", "cache_read_nanos"),
        ("cache_write_usd_per_mtok", "cache_write_nanos"),
        ("cache_write_1h_usd_per_mtok", "cache_write_1h_nanos"),
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(route, column, _nanos(value))
    for field in ("enabled", "upstream_model", "description"):
        value = getattr(body, field)
        if value is not None:
            setattr(route, field, value)
    await db.commit()
    ctx(request).gateway.invalidate()
    return route_out(route)


@admin.delete("/models/{route_id}")
async def admin_delete_model(route_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    route = await db.get(ModelRoute, route_id)
    if route is None or route.org_id is not None:
        raise HTTPException(404, "Model not found")
    await db.delete(route)
    await db.commit()
    ctx(request).gateway.invalidate()
    return {"deleted": route_id}
