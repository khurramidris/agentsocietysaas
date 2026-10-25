"""Plans, the prepaid credit ledger and Stripe integration.

Money is integer micro-USD everywhere (1 USD == 1_000_000). The organisation row
holds the authoritative balance; ``credit_ledger`` records every grant, top-up
and adjustment, and ``usage_events`` records every charge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CreditLedger, Organization
from .settings import Settings

MICROS = 1_000_000


@dataclass(frozen=True)
class Plan:
    id: str
    label: str
    monthly_credit_usd: float
    max_projects: int
    max_running_sandboxes: int
    sandbox_sizes: tuple[str, ...]
    max_llm_concurrency: int
    idle_stop_minutes: int
    price_usd_month: float | None


PLANS: dict[str, Plan] = {
    "free": Plan("free", "Free", 0.0, 3, 1, ("small",), 8, 15, 0.0),
    "pro": Plan("pro", "Pro", 40.0, 25, 3, ("small", "medium", "large"), 64, 60, 49.0),
    "team": Plan("team", "Team", 150.0, 200, 10, ("small", "medium", "large", "xlarge"), 256, 120, 199.0),
    "enterprise": Plan("enterprise", "Enterprise", 0.0, 10_000, 100, ("small", "medium", "large", "xlarge"), 2048, 480, None),
}

SANDBOX_SIZES: dict[str, dict[str, Any]] = {
    "small": {"cpus": 2, "memory_gb": 4},
    "medium": {"cpus": 4, "memory_gb": 8},
    "large": {"cpus": 8, "memory_gb": 16},
    "xlarge": {"cpus": 16, "memory_gb": 32},
}


def plan_for(org: Organization) -> Plan:
    return PLANS.get(org.plan, PLANS["free"])


def usd_to_micros(usd: float) -> int:
    return round(usd * MICROS)


async def adjust_balance(
    db: AsyncSession,
    org_id: str,
    delta_micros: int,
    kind: str,
    description: str = "",
    *,
    ref: str | None = None,
    created_by: str | None = None,
) -> CreditLedger | None:
    """Atomically change an org's balance and record it. Idempotent on ``ref``."""
    if ref is not None:
        existing = (await db.execute(select(CreditLedger.id).where(CreditLedger.ref == ref))).scalar_one_or_none()
        if existing is not None:
            return None
    result = await db.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(balance_micros=Organization.balance_micros + delta_micros)
        .returning(Organization.balance_micros)
    )
    balance_after = result.scalar_one()
    entry = CreditLedger(
        org_id=org_id,
        delta_micros=delta_micros,
        kind=kind,
        description=description[:500],
        ref=ref,
        created_by=created_by,
        balance_after=balance_after,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:  # concurrent duplicate ref
        await db.rollback()
        return None
    return entry


async def charge_usage(db: AsyncSession, org_id: str, cost_micros: int) -> None:
    """Debit metered usage (details live in usage_events, not the ledger)."""
    if cost_micros:
        await db.execute(update(Organization).where(Organization.id == org_id).values(balance_micros=Organization.balance_micros - cost_micros))


# ------------------------------------------------------------------------- Stripe


class StripeError(RuntimeError):
    pass


class Stripe:
    """Minimal Stripe REST client: Checkout Sessions + webhook signature verification."""

    api = "https://api.stripe.com/v1"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.stripe_secret_key)

    async def _post(self, path: str, form: list[tuple[str, str]]) -> dict:
        # Stripe takes bracketed form keys; encode explicitly (httpx's ``data=`` wants a mapping).
        resp = await self.client.post(
            f"{self.api}{path}",
            content=urlencode(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(self.settings.stripe_secret_key, ""),
        )
        if resp.status_code >= 400:
            raise StripeError(f"Stripe {path} failed: {resp.status_code} {resp.text[:500]}")
        return resp.json()

    async def ensure_customer(self, org: Organization, email: str) -> str:
        if org.stripe_customer_id:
            return org.stripe_customer_id
        customer = await self._post("/customers", [("email", email), ("name", org.name), ("metadata[org_id]", org.id)])
        org.stripe_customer_id = customer["id"]
        return customer["id"]

    async def topup_session(self, org: Organization, email: str, amount_usd: int, success_url: str, cancel_url: str) -> str:
        customer = await self.ensure_customer(org, email)
        session = await self._post(
            "/checkout/sessions",
            [
                ("mode", "payment"),
                ("customer", customer),
                ("success_url", success_url),
                ("cancel_url", cancel_url),
                ("line_items[0][quantity]", "1"),
                ("line_items[0][price_data][currency]", "usd"),
                ("line_items[0][price_data][unit_amount]", str(amount_usd * 100)),
                ("line_items[0][price_data][product_data][name]", f"{self.settings.brand_name} credits"),
                ("metadata[org_id]", org.id),
                ("metadata[kind]", "topup"),
                ("metadata[credit_usd]", str(amount_usd)),
            ],
        )
        return session["url"]

    async def subscription_session(self, org: Organization, email: str, plan: str, success_url: str, cancel_url: str) -> str:
        price = {"pro": self.settings.stripe_price_pro, "team": self.settings.stripe_price_team}.get(plan)
        if not price:
            raise StripeError(f"No Stripe price configured for plan {plan!r}")
        customer = await self.ensure_customer(org, email)
        session = await self._post(
            "/checkout/sessions",
            [
                ("mode", "subscription"),
                ("customer", customer),
                ("success_url", success_url),
                ("cancel_url", cancel_url),
                ("line_items[0][quantity]", "1"),
                ("line_items[0][price]", price),
                ("metadata[org_id]", org.id),
                ("metadata[kind]", "subscription"),
                ("metadata[plan]", plan),
                ("subscription_data[metadata][org_id]", org.id),
                ("subscription_data[metadata][plan]", plan),
            ],
        )
        return session["url"]


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance_s: int = 300) -> dict:
    """Verify a ``Stripe-Signature`` header (v1 scheme) and return the parsed event."""
    if not secret:
        raise StripeError("Webhook secret not configured")
    items = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp = items.get("t")
    signatures = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not timestamp or not signatures:
        raise StripeError("Malformed Stripe-Signature header")
    if abs(time.time() - int(timestamp)) > tolerance_s:
        raise StripeError("Stripe signature timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise StripeError("Invalid Stripe signature")
    return json.loads(payload)


async def handle_stripe_event(db: AsyncSession, event: dict) -> str:
    """Apply a verified Stripe event. Returns a short outcome string (for logs/tests)."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    org_id = meta.get("org_id")
    if etype == "checkout.session.completed":
        if not org_id or obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored"
        org = await db.get(Organization, org_id)
        if org is None:
            return "unknown-org"
        if meta.get("kind") == "topup":
            credit = usd_to_micros(float(meta.get("credit_usd", "0")))
            await adjust_balance(db, org_id, credit, "topup", "Stripe credit top-up", ref=f"stripe:{event['id']}")
            await db.commit()
            return "topup"
        if meta.get("kind") == "subscription" and meta.get("plan") in PLANS:
            org.plan = meta["plan"]
            org.stripe_subscription_id = obj.get("subscription")
            await db.commit()
            return f"plan:{org.plan}"
        return "ignored"
    if etype == "invoice.paid":
        # API versions from 2025-03-31 nest this under invoice.parent.subscription_details.
        details = obj.get("subscription_details") or (obj.get("parent") or {}).get("subscription_details") or {}
        sub_meta = details.get("metadata") or {}
        org_id = sub_meta.get("org_id") or org_id
        plan = PLANS.get(sub_meta.get("plan", ""))
        if not org_id or plan is None or not plan.monthly_credit_usd:
            return "ignored"
        await adjust_balance(
            db,
            org_id,
            usd_to_micros(plan.monthly_credit_usd),
            "subscription",
            f"{plan.label} monthly credit",
            ref=f"stripe:{event['id']}",
        )
        await db.commit()
        return "monthly-credit"
    if etype in ("customer.subscription.deleted", "customer.subscription.updated"):
        org = await db.get(Organization, org_id) if org_id else None
        if org is None:
            return "ignored"
        if org.stripe_subscription_id and obj.get("id") and obj["id"] != org.stripe_subscription_id:
            return "ignored"  # an older subscription of this org
        if etype == "customer.subscription.updated" and obj.get("status") in ("active", "trialing", "past_due"):
            plan = meta.get("plan")
            if plan in PLANS and plan != org.plan:
                org.plan = plan
                await db.commit()
                return f"plan:{plan}"
            return "ignored"
        if etype == "customer.subscription.updated" and obj.get("status") not in ("canceled", "unpaid", "incomplete_expired"):
            return "ignored"
        org.plan = "free"
        org.stripe_subscription_id = None
        await db.commit()
        return "downgraded"
    return "ignored"
