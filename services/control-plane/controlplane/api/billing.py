"""Credits, plans, Stripe checkout/webhooks and usage analytics."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import audit
from ..billing import PLANS, StripeError, handle_stripe_event, verify_stripe_signature
from ..db import utcnow
from ..deps import Principal, ctx, get_db, get_principal, org_access
from ..models import CreditLedger, Project, UsageEvent

router = APIRouter(tags=["billing"])


def _usd(micros: int | None) -> float:
    return round(int(micros or 0) / 1_000_000, 6)


@router.get("/api/v1/orgs/{org_id}/billing")
async def billing_overview(org_id: str, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id)
    ledger = (await db.execute(select(CreditLedger).where(CreditLedger.org_id == org_id).order_by(CreditLedger.created_at.desc()).limit(50))).scalars().all()
    since = utcnow() - timedelta(days=30)
    spent = (await db.execute(select(func.sum(UsageEvent.cost_micros)).where(UsageEvent.org_id == org_id, UsageEvent.created_at >= since))).scalar_one()
    pending = ctx(request).usage.pending_for(org_id)
    return {
        "org_id": org_id,
        "plan": access.org.plan,
        "balance_usd": _usd(access.org.balance_micros - pending),
        "spent_30d_usd": _usd((spent or 0) + pending),
        "stripe_enabled": ctx(request).stripe.enabled,
        "plans": [
            {
                "id": p.id,
                "label": p.label,
                "price_usd_month": p.price_usd_month,
                "monthly_credit_usd": p.monthly_credit_usd,
                "max_projects": p.max_projects,
                "max_running_sandboxes": p.max_running_sandboxes,
                "sandbox_sizes": list(p.sandbox_sizes),
                "max_llm_concurrency": p.max_llm_concurrency,
            }
            for p in PLANS.values()
        ],
        "ledger": [
            {
                "id": e.id,
                "delta_usd": _usd(e.delta_micros),
                "kind": e.kind,
                "description": e.description,
                "balance_after_usd": _usd(e.balance_after),
                "created_at": e.created_at,
            }
            for e in ledger
        ],
    }


class TopupBody(BaseModel):
    amount_usd: int = Field(..., ge=5, le=10_000)


@router.post("/api/v1/orgs/{org_id}/billing/topup")
async def topup(org_id: str, body: TopupBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "admin")
    stripe = ctx(request).stripe
    if not stripe.enabled:
        raise HTTPException(400, "Online payments are not configured on this deployment")
    base = ctx(request).settings.public_base_url
    email = principal.user.email if principal.user else ""
    try:
        url = await stripe.topup_session(access.org, email, body.amount_usd, f"{base}/org/billing?org={org_id}&paid=1", f"{base}/org/billing?org={org_id}")
    except StripeError as exc:
        raise HTTPException(502, str(exc)) from None
    await db.commit()
    return {"checkout_url": url}


class SubscribeBody(BaseModel):
    plan: str


@router.post("/api/v1/orgs/{org_id}/billing/subscribe")
async def subscribe(org_id: str, body: SubscribeBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    access = await org_access(db, principal, org_id, "owner")
    stripe = ctx(request).stripe
    if not stripe.enabled:
        raise HTTPException(400, "Online payments are not configured on this deployment")
    base = ctx(request).settings.public_base_url
    try:
        url = await stripe.subscription_session(
            access.org,
            principal.user.email if principal.user else "",
            body.plan,
            f"{base}/org/billing?org={org_id}&subscribed=1",
            f"{base}/org/billing?org={org_id}",
        )
    except StripeError as exc:
        raise HTTPException(400, str(exc)) from None
    await db.commit()
    return {"checkout_url": url}


@router.post("/api/v1/billing/stripe/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    try:
        event = verify_stripe_signature(payload, request.headers.get("stripe-signature", ""), ctx(request).settings.stripe_webhook_secret)
    except StripeError as exc:
        raise HTTPException(400, str(exc)) from None
    outcome = await handle_stripe_event(db, event)
    if outcome not in ("ignored",):
        audit(db, request, "billing.stripe_event", target_type="stripe_event", target_id=event.get("id", ""), outcome=outcome)
        await db.commit()
    return {"received": True, "outcome": outcome}


@router.get("/api/v1/orgs/{org_id}/usage")
async def usage(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db), days: int = Query(30, ge=1, le=365)):
    await org_access(db, principal, org_id)
    since = utcnow() - timedelta(days=days)
    base = select().where(UsageEvent.org_id == org_id, UsageEvent.created_at >= since)
    day = func.date(UsageEvent.created_at)
    by_day = (await db.execute(base.with_only_columns(day, func.sum(UsageEvent.cost_micros), func.count(UsageEvent.id)).group_by(day).order_by(day))).all()
    by_model = (
        await db.execute(
            base.with_only_columns(
                UsageEvent.kind,
                UsageEvent.model,
                func.count(UsageEvent.id),
                func.sum(UsageEvent.input_tokens),
                func.sum(UsageEvent.output_tokens),
                func.sum(UsageEvent.cached_tokens),
                func.sum(UsageEvent.quantity),
                func.sum(UsageEvent.cost_micros),
            ).group_by(UsageEvent.kind, UsageEvent.model)
        )
    ).all()
    by_project = (
        await db.execute(
            base.with_only_columns(UsageEvent.project_id, func.sum(UsageEvent.cost_micros), func.count(UsageEvent.id)).group_by(UsageEvent.project_id)
        )
    ).all()
    names = dict((await db.execute(select(Project.id, Project.name).where(Project.org_id == org_id))).all())
    return {
        "days": days,
        "by_day": [{"date": str(d), "cost_usd": _usd(c), "requests": int(n)} for d, c, n in by_day],
        "by_model": [
            {
                "kind": k,
                "model": m,
                "requests": int(n),
                "input_tokens": int(i or 0),
                "output_tokens": int(o or 0),
                "cached_tokens": int(c or 0),
                "seconds": int(q or 0),
                "cost_usd": _usd(cost),
            }
            for k, m, n, i, o, c, q, cost in by_model
        ],
        "by_project": [{"project_id": p, "project_name": names.get(p, "API / other"), "cost_usd": _usd(c), "requests": int(n)} for p, c, n in by_project],
    }
