"""Organizations, members, invitations and API keys."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..billing import plan_for
from ..db import utcnow
from ..deps import Principal, ctx, get_db, get_principal, get_user, org_access
from ..models import ROLE_RANK, ROLES, ApiKey, Invitation, Membership, Organization, Project, User
from ..security import API_KEY_PREFIX, new_composite, random_token, sha256_hex
from .auth import unique_org_slug

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])


def org_out(org: Organization, role: str | None = None) -> dict:
    plan = plan_for(org)
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "plan": org.plan,
        "plan_label": plan.label,
        "is_personal": org.is_personal,
        "balance_usd": round(org.balance_micros / 1_000_000, 6),
        "role": role,
        "limits": {
            "max_projects": plan.max_projects,
            "max_running_sandboxes": plan.max_running_sandboxes,
            "sandbox_sizes": list(plan.sandbox_sizes),
            "max_llm_concurrency": plan.max_llm_concurrency,
            "idle_stop_minutes": plan.idle_stop_minutes,
        },
        "created_at": org.created_at,
    }


class CreateOrg(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.get("")
async def list_orgs(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Membership, Organization).join(Organization).where(Membership.user_id == user.id))).all()
    return {"orgs": [org_out(org, m.role) for m, org in rows]}


@router.post("")
async def create_org(body: CreateOrg, request: Request, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    org = Organization(name=body.name, slug=await unique_org_slug(db, body.name))
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    audit(db, request, "org.created", org_id=org.id, user_id=user.id, target_type="org", target_id=org.id)
    await db.commit()
    return org_out(org, "owner")


@router.get("/{org_id}")
async def get_org(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id)
    projects = (await db.execute(select(func.count(Project.id)).where(Project.org_id == org_id, Project.archived_at.is_(None)))).scalar_one()
    return {**org_out(access.org, access.role), "project_count": projects}


class UpdateOrg(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@router.patch("/{org_id}")
async def update_org(org_id: str, body: UpdateOrg, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "admin")
    access.org.name = body.name
    audit(db, request, "org.renamed", org_id=org_id, user_id=principal.user_id, name=body.name)
    await db.commit()
    return org_out(access.org, access.role)


# ------------------------------------------------------------------- members


@router.get("/{org_id}/members")
async def list_members(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    rows = (await db.execute(select(Membership, User).join(User).where(Membership.org_id == org_id))).all()
    invites = (
        (await db.execute(select(Invitation).where(Invitation.org_id == org_id, Invitation.accepted_at.is_(None), Invitation.expires_at > utcnow())))
        .scalars()
        .all()
    )
    return {
        "members": [{"user_id": u.id, "email": u.email, "name": u.name, "role": m.role, "joined_at": m.created_at} for m, u in rows],
        "invitations": [{"id": i.id, "email": i.email, "role": i.role, "expires_at": i.expires_at} for i in invites],
    }


class RoleBody(BaseModel):
    role: str


async def _owner_count(db: AsyncSession, org_id: str) -> int:
    return (await db.execute(select(func.count(Membership.id)).where(Membership.org_id == org_id, Membership.role == "owner"))).scalar_one()


async def _revoke_member_keys(db: AsyncSession, org_id: str, user_id: str, max_role: str | None) -> int:
    """Revoke the member's API keys in this org that exceed ``max_role`` (all when None)."""
    keys = (await db.execute(select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None)))).scalars().all()
    revoked = 0
    for key in keys:
        if max_role is None or ROLE_RANK[key.role] > ROLE_RANK[max_role]:
            key.revoked_at = utcnow()
            revoked += 1
    return revoked


@router.patch("/{org_id}/members/{user_id}")
async def change_role(
    org_id: str, user_id: str, body: RoleBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)
):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    access = await org_access(db, principal, org_id, "admin")
    member = (await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Member not found")
    if "owner" in (body.role, member.role) and access.role != "owner":
        raise HTTPException(403, "Only owners can grant or remove the owner role")
    if member.role == "owner" and body.role != "owner" and await _owner_count(db, org_id) <= 1:
        raise HTTPException(409, "An organization needs at least one owner")
    member.role = body.role
    revoked = await _revoke_member_keys(db, org_id, user_id, body.role)
    audit(
        db,
        request,
        "org.member_role_changed",
        org_id=org_id,
        user_id=principal.user_id,
        target_type="user",
        target_id=user_id,
        role=body.role,
        api_keys_revoked=revoked,
    )
    await db.commit()
    if revoked:
        ctx(request).gateway.invalidate()
    return {"user_id": user_id, "role": body.role, "api_keys_revoked": revoked}


@router.delete("/{org_id}/members/{user_id}")
async def remove_member(org_id: str, user_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    is_self = principal.user_id == user_id
    access = await org_access(db, principal, org_id, "viewer" if is_self else "admin")
    member = (await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == "owner":
        if not is_self and access.role != "owner":
            raise HTTPException(403, "Only owners can remove an owner")
        if await _owner_count(db, org_id) <= 1:
            raise HTTPException(409, "An organization needs at least one owner")
    await db.delete(member)
    revoked = await _revoke_member_keys(db, org_id, user_id, None)
    audit(db, request, "org.member_removed", org_id=org_id, user_id=principal.user_id, target_type="user", target_id=user_id, api_keys_revoked=revoked)
    await db.commit()
    if revoked:
        ctx(request).gateway.invalidate()
    return {"removed": user_id, "api_keys_revoked": revoked}


class InviteBody(BaseModel):
    email: EmailStr
    role: str = "member"


@router.post("/{org_id}/invitations")
async def invite(org_id: str, body: InviteBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "admin")
    if body.role not in ROLES or (body.role == "owner" and access.role != "owner"):
        raise HTTPException(400, "Invalid role for invitation")
    raw = random_token(32)
    inv = Invitation(
        org_id=org_id,
        email=body.email.lower(),
        role=body.role,
        token_hash=sha256_hex(raw),
        invited_by=principal.user_id,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(inv)
    audit(db, request, "org.member_invited", org_id=org_id, user_id=principal.user_id, target_type="invitation", email=body.email.lower(), role=body.role)
    await db.commit()
    link = f"{ctx(request).settings.public_base_url}/invite/{raw}"
    # Email delivery is pluggable; the link is returned so admins can share it directly.
    return {"id": inv.id, "email": inv.email, "role": inv.role, "accept_url": link, "token": raw, "expires_at": inv.expires_at}


@router.delete("/{org_id}/invitations/{invitation_id}")
async def revoke_invitation(org_id: str, invitation_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    inv = await db.get(Invitation, invitation_id)
    if inv is None or inv.org_id != org_id:
        raise HTTPException(404, "Invitation not found")
    await db.delete(inv)
    await db.commit()
    return {"revoked": invitation_id}


# ------------------------------------------------------------------ API keys


class CreateKey(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    role: str = "member"


def key_out(key: ApiKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": f"{API_KEY_PREFIX}_{key.prefix}",
        "role": key.role,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
    }


@router.get("/{org_id}/api-keys")
async def list_keys(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    keys = (await db.execute(select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc()))).scalars().all()
    return {"api_keys": [key_out(k) for k in keys]}


@router.post("/{org_id}/api-keys")
async def create_key(org_id: str, body: CreateKey, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "admin")
    if body.role not in ROLES or ROLE_RANK[body.role] > ROLE_RANK[access.role] or body.role == "owner":
        raise HTTPException(400, "Invalid role for an API key")
    token = new_composite(API_KEY_PREFIX)
    key = ApiKey(org_id=org_id, user_id=principal.user_id, name=body.name, prefix=token.ident, secret_hash=sha256_hex(token.secret), role=body.role)
    db.add(key)
    audit(db, request, "org.api_key_created", org_id=org_id, user_id=principal.user_id, target_type="api_key", target_id=key.id, name=body.name)
    await db.commit()
    return {**key_out(key), "secret": token.value}


@router.delete("/{org_id}/api-keys/{key_id}")
async def revoke_key(org_id: str, key_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "admin")
    key = await db.get(ApiKey, key_id)
    if key is None or key.org_id != org_id:
        raise HTTPException(404, "API key not found")
    key.revoked_at = utcnow()
    audit(db, request, "org.api_key_revoked", org_id=org_id, user_id=principal.user_id, target_type="api_key", target_id=key_id)
    await db.commit()
    ctx(request).gateway.invalidate()
    return key_out(key)
