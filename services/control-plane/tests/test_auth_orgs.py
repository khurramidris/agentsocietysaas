"""Accounts, sessions, organizations, roles, invitations and API keys."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from controlplane.models import AuditLog, CreditLedger

from .conftest import PASSWORD


async def test_signup_creates_personal_org_with_welcome_credit(cp):
    alice = await cp.signup("Alice@Example.com", name="Alice")
    r = await cp.client.get("/api/v1/auth/me", headers=alice.h)
    assert r.status_code == 200
    me = r.json()
    assert me["user"]["email"] == "alice@example.com"  # normalised
    assert me["orgs"] == [{"id": alice.org_id, "name": "Alice's workspace", "slug": "alice-s-workspace", "plan": "free", "role": "owner", "is_personal": True}]
    org = (await cp.client.get(f"/api/v1/orgs/{alice.org_id}", headers=alice.h)).json()
    assert org["balance_usd"] == 5.0
    async with cp.ctx.db.sessionmaker() as db:
        ledger = (await db.execute(select(CreditLedger).where(CreditLedger.org_id == alice.org_id))).scalars().all()
        assert [(e.kind, e.delta_micros, e.ref) for e in ledger] == [("grant", 5_000_000, f"signup:{alice.user_id}")]
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
        assert "user.signup" in actions


async def test_signup_rejects_duplicates_and_respects_toggle(cp):
    await cp.signup("bob@example.com")
    r = await cp.client.post("/api/v1/auth/signup", json={"email": "BOB@example.com", "password": PASSWORD})
    assert r.status_code == 409
    r = await cp.client.post("/api/v1/auth/signup", json={"email": "short@example.com", "password": "x"})
    assert r.status_code == 422
    cp.settings.allow_signup = False
    r = await cp.client.post("/api/v1/auth/signup", json={"email": "carol@example.com", "password": PASSWORD})
    assert r.status_code == 403


async def test_login_refresh_rotation_and_logout(cp):
    await cp.signup("dana@example.com")
    assert (await cp.login("dana@example.com", "wrong-password")).status_code == 401
    r = await cp.login("dana@example.com")
    assert r.status_code == 200
    first = r.cookies["cp_rt"]
    assert "httponly" in r.headers["set-cookie"].lower() and "path=/api/v1/auth" in r.headers["set-cookie"].lower()

    async def refresh_with(cookie: str):
        cp.client.cookies = httpx.Cookies({"cp_rt": cookie})
        return await cp.client.post("/api/v1/auth/refresh")

    r = await refresh_with(first)
    assert r.status_code == 200 and r.json()["access_token"]
    second = r.cookies["cp_rt"]
    assert second != first
    # Refresh tokens rotate: the used one is dead.
    assert (await refresh_with(first)).status_code == 401

    cp.client.cookies = httpx.Cookies({"cp_rt": second})
    assert (await cp.client.post("/api/v1/auth/logout")).status_code == 200
    assert (await refresh_with(second)).status_code == 401


async def test_login_is_rate_limited(cp):
    await cp.signup("eve@example.com")
    codes = [(await cp.login("eve@example.com", "nope-nope")).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


async def test_password_change_requires_current_password(cp):
    frank = await cp.signup("frank@example.com")
    r = await cp.client.patch("/api/v1/auth/me", json={"new_password": "brand-new-pass", "current_password": "wrong"}, headers=frank.h)
    assert r.status_code == 400
    r = await cp.client.patch("/api/v1/auth/me", json={"new_password": "brand-new-pass", "current_password": PASSWORD, "name": "Frank"}, headers=frank.h)
    assert r.status_code == 200 and r.json()["user"]["name"] == "Frank"
    assert (await cp.login("frank@example.com")).status_code == 401
    assert (await cp.login("frank@example.com", "brand-new-pass")).status_code == 200


async def test_requests_without_valid_token_are_rejected(cp):
    assert (await cp.client.get("/api/v1/auth/me")).status_code == 401
    assert (await cp.client.get("/api/v1/auth/me", headers={"authorization": "Bearer nonsense"})).status_code == 401
    alice = await cp.signup("alice@example.com")
    # Another user's org is invisible (404, not 403).
    bob = await cp.signup("bob@example.com")
    assert (await cp.client.get(f"/api/v1/orgs/{alice.org_id}", headers=bob.h)).status_code == 404


async def test_invitations_and_roles(cp):
    owner = await cp.signup("owner@example.com")
    member = await cp.signup("member@example.com")
    viewer = await cp.signup("viewer@example.com")
    stranger = await cp.signup("stranger@example.com")
    team = (await cp.client.post("/api/v1/orgs", json={"name": "Lab Team"}, headers=owner.h)).json()
    assert team["role"] == "owner" and team["is_personal"] is False and team["balance_usd"] == 0
    owner.org_id = team["id"]

    r = await cp.client.post(f"/api/v1/orgs/{team['id']}/invitations", json={"email": "member@example.com", "role": "member"}, headers=owner.h)
    invite = r.json()
    assert invite["accept_url"].startswith("http://testserver/invite/")
    # Only the invited address can accept.
    r = await cp.client.post("/api/v1/auth/invitations/accept", json={"token": invite["token"]}, headers=stranger.h)
    assert r.status_code == 403
    r = await cp.client.post("/api/v1/auth/invitations/accept", json={"token": invite["token"]}, headers=member.h)
    assert r.status_code == 200 and any(o["id"] == team["id"] and o["role"] == "member" for o in r.json()["orgs"])
    # Invitations are single-use.
    assert (await cp.client.post("/api/v1/auth/invitations/accept", json={"token": invite["token"]}, headers=member.h)).status_code == 404

    await cp.add_member(owner, viewer, "viewer")
    members = (await cp.client.get(f"/api/v1/orgs/{team['id']}/members", headers=viewer.h)).json()["members"]
    assert sorted(m["role"] for m in members) == ["member", "owner", "viewer"]

    # Members cannot invite or rename; viewers cannot create projects.
    r = await cp.client.post(f"/api/v1/orgs/{team['id']}/invitations", json={"email": "x@example.com"}, headers=member.h)
    assert r.status_code == 403
    assert (await cp.client.patch(f"/api/v1/orgs/{team['id']}", json={"name": "X"}, headers=member.h)).status_code == 403
    r = await cp.client.post(f"/api/v1/orgs/{team['id']}/projects", json={"name": "p", "start": False}, headers=viewer.h)
    assert r.status_code == 403
    r = await cp.client.post(f"/api/v1/orgs/{team['id']}/projects", json={"name": "p", "start": False}, headers=member.h)
    assert r.status_code == 200

    # Admins cannot hand out ownership; owners can.
    await cp.client.patch(f"/api/v1/orgs/{team['id']}/members/{member.user_id}", json={"role": "admin"}, headers=owner.h)
    r = await cp.client.patch(f"/api/v1/orgs/{team['id']}/members/{viewer.user_id}", json={"role": "owner"}, headers=member.h)
    assert r.status_code == 403
    r = await cp.client.patch(f"/api/v1/orgs/{team['id']}/members/{viewer.user_id}", json={"role": "member"}, headers=member.h)
    assert r.status_code == 200


async def test_last_owner_is_protected(cp):
    owner = await cp.signup("solo@example.com")
    r = await cp.client.patch(f"/api/v1/orgs/{owner.org_id}/members/{owner.user_id}", json={"role": "admin"}, headers=owner.h)
    assert r.status_code == 409
    r = await cp.client.delete(f"/api/v1/orgs/{owner.org_id}/members/{owner.user_id}", headers=owner.h)
    assert r.status_code == 409


async def test_api_keys_scope_and_revocation(cp):
    owner = await cp.signup("keys@example.com")
    other = await cp.signup("other@example.com")
    r = await cp.client.post(f"/api/v1/orgs/{owner.org_id}/api-keys", json={"name": "ci", "role": "owner"}, headers=owner.h)
    assert r.status_code == 400  # owner keys are never issued
    key = await cp.api_key(owner, role="member")
    assert key.startswith("sk_")
    h = {"authorization": f"Bearer {key}"}
    assert (await cp.client.get(f"/api/v1/orgs/{owner.org_id}", headers=h)).status_code == 200
    # A key only works for its own org, and never for user-only endpoints.
    assert (await cp.client.get(f"/api/v1/orgs/{other.org_id}", headers=h)).status_code == 404
    assert (await cp.client.get("/api/v1/auth/me", headers=h)).status_code == 403
    # Member keys cannot manage keys.
    assert (await cp.client.get(f"/api/v1/orgs/{owner.org_id}/api-keys", headers=h)).status_code == 403

    keys = (await cp.client.get(f"/api/v1/orgs/{owner.org_id}/api-keys", headers=owner.h)).json()["api_keys"]
    assert keys[0]["prefix"].startswith("sk_") and "secret" not in keys[0]
    assert (await cp.client.delete(f"/api/v1/orgs/{owner.org_id}/api-keys/{keys[0]['id']}", headers=owner.h)).status_code == 200
    assert (await cp.client.get(f"/api/v1/orgs/{owner.org_id}", headers=h)).status_code == 401

    async with cp.ctx.db.sessionmaker() as db:
        created = (await db.execute(select(AuditLog).where(AuditLog.action == "org.api_key_created"))).scalar_one()
        assert created.target_id == keys[0]["id"]  # ids exist before flush


async def test_removing_or_demoting_a_member_revokes_their_keys(cp):
    owner = await cp.signup("lead@example.com")
    admin = await cp.signup("admin@example.com")
    await cp.add_member(owner, admin, "admin")
    admin_key = await cp.api_key(admin, role="admin", org_id=owner.org_id)
    member_key = await cp.api_key(admin, role="member", org_id=owner.org_id)

    r = await cp.client.patch(f"/api/v1/orgs/{owner.org_id}/members/{admin.user_id}", json={"role": "member"}, headers=owner.h)
    assert r.json()["api_keys_revoked"] == 1
    org_url = f"/api/v1/orgs/{owner.org_id}"
    assert (await cp.client.get(org_url, headers={"authorization": f"Bearer {admin_key}"})).status_code == 401
    assert (await cp.client.get(org_url, headers={"authorization": f"Bearer {member_key}"})).status_code == 200

    r = await cp.client.delete(f"/api/v1/orgs/{owner.org_id}/members/{admin.user_id}", headers=owner.h)
    assert r.json()["api_keys_revoked"] == 1
    assert (await cp.client.get(org_url, headers={"authorization": f"Bearer {member_key}"})).status_code == 401


async def test_members_can_leave(cp):
    owner = await cp.signup("host@example.com")
    guest = await cp.signup("guest@example.com")
    await cp.add_member(owner, guest, "viewer")
    assert (await cp.client.delete(f"/api/v1/orgs/{owner.org_id}/members/{guest.user_id}", headers=guest.h)).status_code == 200
    assert (await cp.client.get(f"/api/v1/orgs/{owner.org_id}", headers=guest.h)).status_code == 404


async def test_platform_admin_bootstrap_and_admin_api(tmp_path, template_db, driver):
    from controlplane.app import create_app

    from .conftest import Lifespan, make_settings

    settings = make_settings(tmp_path, template_db, bootstrap_admin_email="root@example.com", bootstrap_admin_password="root-password-1")
    app = create_app(settings, sandbox_driver=driver)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.post("/api/v1/auth/login", json={"email": "root@example.com", "password": "root-password-1"})).json()["access_token"]
        h = {"authorization": f"Bearer {token}"}
        stats = (await client.get("/api/v1/admin/stats", headers=h)).json()
        assert stats["users"] == 1 and stats["orgs"] == 1
        user = (await client.post("/api/v1/auth/signup", json={"email": "u@example.com", "password": PASSWORD})).json()
        uh = {"authorization": f"Bearer {user['access_token']}"}
        assert (await client.get("/api/v1/admin/stats", headers=uh)).status_code == 403
        org_id = user["orgs"][0]["id"]
        r = await client.post(f"/api/v1/admin/orgs/{org_id}/credits", json={"amount_usd": 20, "description": "pilot"}, headers=h)
        assert r.json()["balance_usd"] == 25.0
        r = await client.patch(f"/api/v1/admin/orgs/{org_id}", json={"plan": "pro"}, headers=h)
        assert r.json()["plan"] == "pro"
        # Admins can see into any org (as owner) for support.
        assert (await client.get(f"/api/v1/orgs/{org_id}", headers=h)).json()["role"] == "owner"
        me = (await client.get("/api/v1/auth/me", headers=h)).json()["user"]
        r = await client.patch(f"/api/v1/admin/users/{me['id']}", json={"is_platform_admin": False}, headers=h)
        assert r.status_code == 409
        r = await client.patch(f"/api/v1/admin/users/{user['user']['id']}", json={"is_active": False}, headers=h)
        assert r.status_code == 200
        assert (await client.get("/api/v1/auth/me", headers=uh)).status_code == 401
        events = (await client.get("/api/v1/admin/audit", headers=h)).json()["events"]
        assert {"admin.credits_adjusted", "admin.plan_changed", "admin.user_updated"} <= {e["action"] for e in events}


async def test_meta_is_public(cp):
    r = await cp.client.get("/api/v1/meta")
    assert r.status_code == 200
    meta = r.json()
    assert meta["brand_name"] == "Societas" and meta["features"]["mock_models"] is True
    assert meta["upstream"]["license"] == "Apache-2.0"
    assert (await cp.client.get("/healthz")).json()["status"] == "ok"
