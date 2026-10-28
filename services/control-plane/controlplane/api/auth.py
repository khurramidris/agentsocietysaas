"""Accounts and sessions: signup, login, refresh, logout, profile, invitations."""

from __future__ import annotations

import re
import secrets
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..billing import adjust_balance, usd_to_micros
from ..db import as_utc, utcnow
from ..deps import client_ip, ctx, get_db, get_user
from ..models import Invitation, Membership, Organization, RefreshToken, User
from ..security import create_access_token, hash_password, random_token, sha256_hex, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = "cp_rt"
_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 10
WINDOW_S = 300


def _check_rate(key: str) -> None:
    """Refuse once ``key`` has used up its attempts in the current window."""
    now = time.time()
    hits = [t for t in _attempts.get(key, ()) if now - t < WINDOW_S]
    if hits:
        _attempts[key] = hits
    else:
        _attempts.pop(key, None)
    if len(hits) >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many attempts; try again in a few minutes")


def _rate_limit(key: str) -> None:
    """Count an attempt against ``key``."""
    _check_rate(key)
    _attempts[key].append(time.time())


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return slug or "org"


async def unique_org_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug = base
    while (await db.execute(select(Organization.id).where(Organization.slug == slug))).first() is not None:
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "is_platform_admin": user.is_platform_admin,
        "created_at": user.created_at,
    }


async def orgs_for(db: AsyncSession, user: User) -> list[dict]:
    rows = (
        await db.execute(select(Membership, Organization).join(Organization, Membership.org_id == Organization.id).where(Membership.user_id == user.id))
    ).all()
    return [{"id": org.id, "name": org.name, "slug": org.slug, "plan": org.plan, "role": m.role, "is_personal": org.is_personal} for m, org in rows]


async def _issue_session(request: Request, response: Response, db: AsyncSession, user: User) -> dict:
    settings = ctx(request).settings
    raw = random_token(48)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=sha256_hex(raw),
            expires_at=utcnow() + timedelta(seconds=settings.refresh_token_ttl_s),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    )
    user.last_login_at = utcnow()
    await db.commit()
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_token_ttl_s,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="strict",
        path="/api/v1/auth",
    )
    return {
        "access_token": create_access_token(settings, user.id),
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_s,
        "user": user_out(user),
        "orgs": await orgs_for(db, user),
    }


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    name: str = Field("", max_length=200)
    org_name: str | None = Field(None, max_length=200)


async def create_user_with_org(db: AsyncSession, settings, *, email: str, password: str, name: str, org_name: str | None, admin: bool = False) -> User:
    user = User(email=email.lower(), name=name, password_hash=hash_password(password), is_platform_admin=admin)
    db.add(user)
    await db.flush()
    org_label = org_name or f"{name or email.split('@')[0]}'s workspace"
    org = Organization(name=org_label, slug=await unique_org_slug(db, org_label), is_personal=org_name is None)
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    if settings.signup_credit_usd > 0:
        await adjust_balance(db, org.id, usd_to_micros(settings.signup_credit_usd), "grant", "Welcome credit", ref=f"signup:{user.id}")
    return user


@router.post("/signup")
async def signup(body: SignupBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    settings = ctx(request).settings
    if not settings.allow_signup:
        raise HTTPException(403, "Sign-up is disabled; ask for an invitation")
    _rate_limit(f"signup:{client_ip(request)}")
    email = body.email.lower()
    if (await db.execute(select(User.id).where(User.email == email))).first() is not None:
        raise HTTPException(409, "An account with this email already exists")
    user = await create_user_with_org(db, settings, email=email, password=body.password, name=body.name, org_name=body.org_name)
    audit(db, request, "user.signup", user_id=user.id, target_type="user", target_id=user.id)
    return await _issue_session(request, response, db, user)


class LoginBody(BaseModel):
    email: EmailStr
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    email = body.email.lower()
    key = f"login:{client_ip(request)}:{email}"
    _check_rate(key)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        _attempts[key].append(time.time())  # only failures count towards the limit
        raise HTTPException(401, "Invalid email or password")
    _attempts.pop(key, None)
    audit(db, request, "user.login", user_id=user.id, target_type="user", target_id=user.id)
    return await _issue_session(request, response, db, user)


@router.post("/refresh")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(401, "No session")
    token = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw)))).scalar_one_or_none()
    if token is None or token.revoked_at is not None or as_utc(token.expires_at) < utcnow():
        response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
        raise HTTPException(401, "Session expired")
    user = await db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "Session expired")
    token.revoked_at = utcnow()  # rotate on every use
    return await _issue_session(request, response, db, user)


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        token = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw)))).scalar_one_or_none()
        if token is not None and token.revoked_at is None:
            token.revoked_at = utcnow()
            await db.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    return {"user": user_out(user), "orgs": await orgs_for(db, user)}


class UpdateMe(BaseModel):
    name: str | None = Field(None, max_length=200)
    current_password: str | None = None
    new_password: str | None = Field(None, min_length=8, max_length=200)


@router.patch("/me")
async def update_me(body: UpdateMe, request: Request, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user.id)
    if body.name is not None:
        user.name = body.name
    if body.new_password:
        if not body.current_password or not verify_password(user.password_hash, body.current_password):
            raise HTTPException(400, "Current password is incorrect")
        user.password_hash = hash_password(body.new_password)
        audit(db, request, "user.password_changed", user_id=user.id, target_type="user", target_id=user.id)
    await db.commit()
    return {"user": user_out(user)}


class AcceptInvite(BaseModel):
    token: str


@router.post("/invitations/accept")
async def accept_invitation(body: AcceptInvite, request: Request, user: User = Depends(get_user), db: AsyncSession = Depends(get_db)):
    inv = (await db.execute(select(Invitation).where(Invitation.token_hash == sha256_hex(body.token)))).scalar_one_or_none()
    if inv is None or inv.accepted_at is not None or as_utc(inv.expires_at) < utcnow():
        raise HTTPException(404, "Invitation not found or expired")
    if inv.email.lower() != user.email.lower():
        raise HTTPException(403, "This invitation was sent to a different email address")
    existing = (await db.execute(select(Membership).where(Membership.org_id == inv.org_id, Membership.user_id == user.id))).scalar_one_or_none()
    if existing is None:
        db.add(Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
    inv.accepted_at = utcnow()
    audit(db, request, "org.invitation_accepted", org_id=inv.org_id, user_id=user.id, target_type="invitation", target_id=inv.id)
    await db.commit()
    return {"org_id": inv.org_id, "orgs": await orgs_for(db, user)}
