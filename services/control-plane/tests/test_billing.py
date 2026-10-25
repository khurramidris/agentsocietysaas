"""Credit ledger, Stripe checkout and webhooks, usage analytics."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select

from controlplane.billing import StripeError, adjust_balance, verify_stripe_signature
from controlplane.models import CreditLedger, Organization

WEBHOOK_SECRET = "whsec_test_secret"
STRIPE = {
    "stripe_secret_key": "stripe-test-key",
    "stripe_webhook_secret": WEBHOOK_SECRET,
    "stripe_price_pro": "price_pro_1",
    "stripe_price_team": "price_team_1",
}


def signed(payload: dict, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = timestamp or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


async def post_event(cp, event: dict, **kw) -> httpx.Response:
    body, header = signed(event, **kw)
    return await cp.client.post("/api/v1/billing/stripe/webhook", content=body, headers={"stripe-signature": header, "content-type": "application/json"})


async def org_row(cp, org_id: str) -> Organization:
    async with cp.ctx.db.sessionmaker() as db:
        return await db.get(Organization, org_id)


def test_signature_verification():
    event = {"id": "evt_1", "type": "x"}
    body, header = signed(event)
    assert verify_stripe_signature(body, header, WEBHOOK_SECRET) == event
    # Rotated secrets: any matching v1 signature is accepted.
    assert verify_stripe_signature(body, header + ",v1=deadbeef", WEBHOOK_SECRET) == event
    with pytest.raises(StripeError):
        verify_stripe_signature(body, header, "whsec_other")
    with pytest.raises(StripeError):
        verify_stripe_signature(body + b" ", header, WEBHOOK_SECRET)
    with pytest.raises(StripeError):
        verify_stripe_signature(*signed(event, timestamp=int(time.time()) - 3600), WEBHOOK_SECRET)
    with pytest.raises(StripeError):
        verify_stripe_signature(body, "garbage", WEBHOOK_SECRET)
    with pytest.raises(StripeError):
        verify_stripe_signature(body, header, "")


async def test_ledger_is_idempotent_on_ref(cp):
    alice = await cp.signup("alice@example.com")
    async with cp.ctx.db.sessionmaker() as db:
        first = await adjust_balance(db, alice.org_id, 3_000_000, "topup", "t", ref="stripe:evt_9")
        await db.commit()
        again = await adjust_balance(db, alice.org_id, 3_000_000, "topup", "t", ref="stripe:evt_9")
        await db.commit()
    assert first is not None and first.balance_after == 8_000_000 and again is None
    assert (await org_row(cp, alice.org_id)).balance_micros == 8_000_000


@pytest.mark.parametrize("cp", [STRIPE], indirect=True)
async def test_topup_checkout_and_webhook(cp):
    alice = await cp.signup("alice@example.com")
    with respx.mock() as mock:
        customers = mock.post("https://api.stripe.com/v1/customers").mock(return_value=httpx.Response(200, json={"id": "cus_123"}))
        sessions = mock.post("https://api.stripe.com/v1/checkout/sessions").mock(
            return_value=httpx.Response(200, json={"url": "https://checkout.stripe.test/s/1"})
        )
        r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/billing/topup", json={"amount_usd": 25}, headers=alice.h)
        assert r.status_code == 200 and r.json()["checkout_url"] == "https://checkout.stripe.test/s/1"
        # The customer is created once and reused.
        await cp.client.post(f"/api/v1/orgs/{alice.org_id}/billing/topup", json={"amount_usd": 10}, headers=alice.h)
    assert customers.call_count == 1
    form = parse_qs(sessions.calls[0].request.content.decode())
    assert form["mode"] == ["payment"] and form["line_items[0][price_data][unit_amount]"] == ["2500"]
    assert form["metadata[org_id]"] == [alice.org_id] and form["metadata[credit_usd]"] == ["25"]
    assert sessions.calls[0].request.headers["authorization"].startswith("Basic ")
    assert (await org_row(cp, alice.org_id)).stripe_customer_id == "cus_123"

    event = {
        "id": "evt_topup_1",
        "type": "checkout.session.completed",
        "data": {"object": {"payment_status": "paid", "metadata": {"org_id": alice.org_id, "kind": "topup", "credit_usd": "25"}}},
    }
    r = await post_event(cp, event)
    assert r.json() == {"received": True, "outcome": "topup"}
    # Stripe retries deliveries: the same event must not credit twice.
    await post_event(cp, event)
    assert (await org_row(cp, alice.org_id)).balance_micros == 30_000_000
    # Unpaid sessions and forged signatures do nothing.
    unpaid = {**event, "id": "evt_topup_2", "data": {"object": {**event["data"]["object"], "payment_status": "unpaid"}}}
    assert (await post_event(cp, unpaid)).json()["outcome"] == "ignored"
    assert (await post_event(cp, {**event, "id": "evt_forged"}, secret="whsec_attacker")).status_code == 400
    assert (await org_row(cp, alice.org_id)).balance_micros == 30_000_000

    overview = (await cp.client.get(f"/api/v1/orgs/{alice.org_id}/billing", headers=alice.h)).json()
    assert overview["balance_usd"] == 30.0 and overview["stripe_enabled"] is True
    assert [e["kind"] for e in overview["ledger"]][:2] == ["topup", "grant"]


@pytest.mark.parametrize("cp", [STRIPE], indirect=True)
async def test_subscription_lifecycle(cp):
    alice = await cp.signup("alice@example.com")
    with respx.mock() as mock:
        mock.post("https://api.stripe.com/v1/customers").mock(return_value=httpx.Response(200, json={"id": "cus_9"}))
        sessions = mock.post("https://api.stripe.com/v1/checkout/sessions").mock(
            return_value=httpx.Response(200, json={"url": "https://checkout.stripe.test/s/2"})
        )
        r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/billing/subscribe", json={"plan": "pro"}, headers=alice.h)
        assert r.status_code == 200
        assert (await cp.client.post(f"/api/v1/orgs/{alice.org_id}/billing/subscribe", json={"plan": "enterprise"}, headers=alice.h)).status_code == 400
    form = parse_qs(sessions.calls[0].request.content.decode())
    assert form["mode"] == ["subscription"] and form["line_items[0][price]"] == ["price_pro_1"]
    assert form["subscription_data[metadata][plan]"] == ["pro"]

    meta = {"org_id": alice.org_id, "plan": "pro"}
    r = await post_event(
        cp,
        {
            "id": "evt_s1",
            "type": "checkout.session.completed",
            "data": {"object": {"payment_status": "paid", "subscription": "sub_1", "metadata": {**meta, "kind": "subscription"}}},
        },
    )
    assert r.json()["outcome"] == "plan:pro"
    org = await org_row(cp, alice.org_id)
    assert org.plan == "pro" and org.stripe_subscription_id == "sub_1"

    # Monthly credit on each paid invoice (both the legacy and the 2025+ invoice shapes).
    r = await post_event(cp, {"id": "evt_i1", "type": "invoice.paid", "data": {"object": {"subscription_details": {"metadata": meta}}}})
    assert r.json()["outcome"] == "monthly-credit"
    r = await post_event(cp, {"id": "evt_i2", "type": "invoice.paid", "data": {"object": {"parent": {"subscription_details": {"metadata": meta}}}}})
    assert r.json()["outcome"] == "monthly-credit"
    assert (await org_row(cp, alice.org_id)).balance_micros == 5_000_000 + 2 * 40_000_000

    # Plan change made in Stripe's portal.
    r = await post_event(
        cp,
        {
            "id": "evt_u1",
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_1", "status": "active", "metadata": {**meta, "plan": "team"}}},
        },
    )
    assert r.json()["outcome"] == "plan:team"
    # Events about some other (old) subscription are ignored.
    r = await post_event(cp, {"id": "evt_u2", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_old", "metadata": meta}}})
    assert r.json()["outcome"] == "ignored" and (await org_row(cp, alice.org_id)).plan == "team"
    r = await post_event(cp, {"id": "evt_d1", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "metadata": meta}}})
    assert r.json()["outcome"] == "downgraded"
    org = await org_row(cp, alice.org_id)
    assert org.plan == "free" and org.stripe_subscription_id is None


async def test_payments_disabled_without_stripe(cp):
    alice = await cp.signup("alice@example.com")
    assert (await cp.client.post(f"/api/v1/orgs/{alice.org_id}/billing/topup", json={"amount_usd": 25}, headers=alice.h)).status_code == 400
    r = await cp.client.post("/api/v1/billing/stripe/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 400


async def test_billing_roles(cp):
    owner = await cp.signup("owner@example.com")
    member = await cp.signup("member@example.com")
    await cp.add_member(owner, member, "member")
    assert (await cp.client.get(f"/api/v1/orgs/{owner.org_id}/billing", headers=member.h)).status_code == 200
    assert (await cp.client.post(f"/api/v1/orgs/{owner.org_id}/billing/topup", json={"amount_usd": 25}, headers=member.h)).status_code == 403
    assert (await cp.client.post(f"/api/v1/orgs/{owner.org_id}/billing/subscribe", json={"plan": "pro"}, headers=member.h)).status_code == 403


async def test_usage_analytics(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice, "Study")
    token = cp.project_token(project["id"])
    key = await cp.api_key(alice)
    for auth in (token, key, key):
        await cp.client.post(
            "/llm/v1/chat/completions", json={"model": "mock", "messages": [{"role": "user", "content": "x"}]}, headers={"authorization": f"Bearer {auth}"}
        )
    await cp.flush_usage()
    usage = (await cp.client.get(f"/api/v1/orgs/{alice.org_id}/usage", headers=alice.h)).json()
    assert usage["by_day"][0]["requests"] == 3
    by_project = {p["project_name"]: p["requests"] for p in usage["by_project"]}
    assert by_project == {"Study": 1, "API / other": 2}
    assert usage["by_model"][0]["model"] == "mock"
    async with cp.ctx.db.sessionmaker() as db:
        assert len((await db.execute(select(CreditLedger))).scalars().all()) == 1  # usage is not in the ledger
