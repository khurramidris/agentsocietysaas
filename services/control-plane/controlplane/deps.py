"""FastAPI dependencies: database session, authenticated principal, role checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import utcnow
from .models import ROLE_RANK, ApiKey, Membership, Organization, Project, User
from .security import API_KEY_PREFIX, decode_access_token, parse_composite, secret_matches

if TYPE_CHECKING:
    from .context import AppContext


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.ctx.db.sessionmaker() as session:
        yield session


@dataclass
class Principal:
    user: User | None = None
    api_key: ApiKey | None = None

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user else (self.api_key.user_id if self.api_key else None)

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user and self.user.is_platform_admin)


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


async def resolve_principal(request: Request, db: AsyncSession) -> Principal | None:
    token = bearer_token(request)
    if not token:
        return None
    settings = request.app.state.ctx.settings
    composite = parse_composite(token)
    if composite and composite.kind == API_KEY_PREFIX:
        key = (await db.execute(select(ApiKey).where(ApiKey.prefix == composite.ident))).scalar_one_or_none()
        if key is None or key.revoked_at is not None or not secret_matches(key.secret_hash, composite.secret):
            return None
        key.last_used_at = utcnow()
        await db.commit()
        return Principal(api_key=key)
    user_id = decode_access_token(settings, token)
    if not user_id:
        return None
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return Principal(user=user)


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    principal = await resolve_principal(request, db)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return principal


async def get_user(principal: Principal = Depends(get_principal)) -> User:
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This endpoint requires a user session, not an API key")
    return principal.user


async def require_platform_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform administrators only")
    return principal


@dataclass
class OrgAccess:
    org: Organization
    role: str
    principal: Principal

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


async def org_access(db: AsyncSession, principal: Principal, org_id: str, min_role: str = "viewer") -> OrgAccess:
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    role: str | None = None
    if principal.api_key is not None:
        if principal.api_key.org_id == org_id:
            role = principal.api_key.role
    elif principal.user is not None:
        membership = (await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.user_id == principal.user.id))).scalar_one_or_none()
        if membership is not None:
            role = membership.role
        elif principal.is_platform_admin:
            role = "owner"
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the {min_role} role in this organization")
    return OrgAccess(org=org, role=role, principal=principal)


@dataclass
class ProjectAccess:
    project: Project
    access: OrgAccess


async def project_access(db: AsyncSession, principal: Principal, project_id: str, min_role: str = "viewer") -> ProjectAccess:
    project = await db.get(Project, project_id)
    if project is None or project.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    access = await org_access(db, principal, project.org_id, min_role)
    return ProjectAccess(project=project, access=access)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]
