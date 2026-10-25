"""Who is calling the gateway, and which provider serves the requested model."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import select

from ..db import utcnow
from ..models import ROLE_RANK, ApiKey, ModelRoute, Project, ProjectToken, Provider
from ..security import API_KEY_PREFIX, PROJECT_TOKEN_PREFIX, parse_composite, secret_matches, sha256_hex

if TYPE_CHECKING:
    from ..context import AppContext

# Revocations invalidate this replica's cache at once; other replicas follow within the TTL.
CALLER_TTL_S = 15.0
ROUTE_TTL_S = 30.0


class GatewayError(Exception):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Caller:
    org_id: str
    project_id: str | None
    api_key_id: str | None
    source: str  # sandbox | api


def extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    for name in ("x-api-key", "api-key"):
        if request.headers.get(name):
            return request.headers[name].strip()
    return None


class CallerResolver:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._cache: dict[str, tuple[float, Caller]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    async def resolve(self, request: Request) -> Caller:
        token = extract_token(request)
        if not token:
            raise GatewayError(401, "authentication_error", "Missing API key")
        cache_key = sha256_hex(token)
        hit = self._cache.get(cache_key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        composite = parse_composite(token)
        if composite is None:
            raise GatewayError(401, "authentication_error", "Invalid API key")
        async with self.ctx.db.sessionmaker() as db:
            if composite.kind == PROJECT_TOKEN_PREFIX:
                row = await db.get(ProjectToken, f"ptk_{composite.ident}")
                if row is None or row.revoked_at is not None or not secret_matches(row.secret_hash, composite.secret):
                    raise GatewayError(401, "authentication_error", "Invalid or revoked project token")
                project = await db.get(Project, row.project_id)
                if project is None or project.archived_at is not None:
                    raise GatewayError(401, "authentication_error", "Project no longer exists")
                caller = Caller(org_id=row.org_id, project_id=row.project_id, api_key_id=None, source="sandbox")
            elif composite.kind == API_KEY_PREFIX:
                key = (await db.execute(select(ApiKey).where(ApiKey.prefix == composite.ident))).scalar_one_or_none()
                if key is None or key.revoked_at is not None or not secret_matches(key.secret_hash, composite.secret):
                    raise GatewayError(401, "authentication_error", "Invalid or revoked API key")
                if ROLE_RANK[key.role] < ROLE_RANK["member"]:
                    raise GatewayError(403, "permission_error", "Viewer keys cannot call models")
                key.last_used_at = utcnow()
                await db.commit()
                caller = Caller(org_id=key.org_id, project_id=None, api_key_id=key.id, source="api")
            else:  # pragma: no cover - parse_composite restricts kinds
                raise GatewayError(401, "authentication_error", "Invalid API key")
        self._cache[cache_key] = (time.monotonic() + CALLER_TTL_S, caller)
        return caller


@dataclass
class ResolvedModel:
    route: ModelRoute | None
    provider: Provider
    upstream_model: str
    billable: bool


class ModelResolver:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._cache: dict[tuple[str, str, str], tuple[float, ResolvedModel]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    async def resolve(self, org_id: str, alias: str, kind: str) -> ResolvedModel:
        if not alias:
            raise GatewayError(400, "invalid_request_error", "model is required")
        key = (org_id, alias, kind)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with self.ctx.db.sessionmaker() as db:
            route = None
            provider = None
            for owner in (org_id, None):
                cond = ModelRoute.org_id == owner if owner else ModelRoute.org_id.is_(None)
                candidate = (await db.execute(select(ModelRoute).where(cond, ModelRoute.alias == alias, ModelRoute.enabled.is_(True)))).scalar_one_or_none()
                if candidate is None:
                    continue
                prov = await db.get(Provider, candidate.provider_id)
                if prov is not None and prov.enabled:
                    route, provider = candidate, prov
                    break
            if route is None and self.ctx.settings.enable_mock_provider:
                provider = (await db.execute(select(Provider).where(Provider.org_id.is_(None), Provider.kind == "mock"))).scalar_one_or_none()
            if provider is None:
                raise GatewayError(404, "not_found_error", f"Model '{alias}' is not available to this organization")
            if route is not None and route.kind != kind:
                raise GatewayError(400, "invalid_request_error", f"Model '{alias}' is a {route.kind} model")
            resolved = ResolvedModel(
                route=route,
                provider=provider,
                upstream_model=route.upstream_model if route else alias,
                billable=bool(route and provider.org_id is None),
            )
        self._cache[key] = (time.monotonic() + ROUTE_TTL_S, resolved)
        return resolved
