"""Projects, sandbox lifecycle, reconciliation, the sandbox proxy and the IDE gate."""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from controlplane.db import utcnow
from controlplane.models import Organization, Project, UsageEvent


async def test_project_start_hands_sandbox_only_scoped_credentials(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice, "Echo chambers", topic="How feeds polarise")
    assert project["slug"] == "echo-chambers" and project["topic"] == "How feeds polarise"

    status = (await cp.client.get(f"/api/v1/projects/{project['id']}/sandbox", headers=alice.h)).json()
    assert status["status"] == "running"
    assert status["agent"]["project_id"] == project["id"]  # answered by the real sandboxd

    env = cp.driver.specs[-1].env
    assert env["PLATFORM_TOKEN"].startswith("sbx_")
    assert env["PLATFORM_LLM_BASE_URL"] == "http://testserver/llm/v1"
    assert env["PLATFORM_ANTHROPIC_BASE_URL"] == "http://testserver/llm/anthropic"
    assert env["PLATFORM_LITERATURE_MCP_URL"] == "http://testserver/literature/mcp/"
    assert env["SANDBOX_TOPIC"] == "How feeds polarise" and env["SANDBOX_SIM_MODEL"] == "mock"
    # Nothing of the control plane's own configuration or secrets leaks into a sandbox.
    assert not [k for k in env if k.startswith("CP_")]
    assert cp.settings.secret_key not in env.values()

    # Slugs stay unique within the org.
    again = await cp.create_project(alice, "Echo chambers", start=False)
    assert again["slug"] == "echo-chambers-2"


async def test_proxy_enforces_roles_and_reaches_the_workspace(cp):
    owner = await cp.signup("owner@example.com")
    viewer = await cp.signup("viewer@example.com")
    stranger = await cp.signup("stranger@example.com")
    await cp.add_member(owner, viewer, "viewer")
    project = await cp.create_project(owner)
    base = f"/api/v1/projects/{project['id']}/sbx"

    r = await cp.client.put(f"{base}/files/content", json={"path": "notes/plan.md", "content": "# Plan\n"}, headers=owner.h)
    assert r.status_code == 200, r.text
    assert (cp.driver.workspace(project["id"]) / "notes" / "plan.md").read_text() == "# Plan\n"

    r = await cp.client.get(f"{base}/files/content", params={"path": "notes/plan.md"}, headers=viewer.h)
    assert r.status_code == 200 and r.json()["content"] == "# Plan\n"
    tree = (await cp.client.get(f"{base}/files/tree", headers=viewer.h)).json()
    assert any(e["name"] == "notes" for e in tree["entries"])

    # Viewers read, members write; outsiders see nothing.
    r = await cp.client.put(f"{base}/files/content", json={"path": "x.md", "content": "x"}, headers=viewer.h)
    assert r.status_code == 403
    assert (await cp.client.get(f"{base}/files/tree", headers=stranger.h)).status_code == 404
    # Sandbox-side protections still apply through the proxy.
    r = await cp.client.put(f"{base}/files/content", json={"path": ".env", "content": "LEAK=1"}, headers=owner.h)
    assert r.status_code in (400, 403)
    r = await cp.client.get(f"{base}/files/content", params={"path": "../../etc/passwd"}, headers=owner.h)
    assert r.status_code in (400, 403, 404)
    # The IDE is only reachable through its ticket flow.
    assert (await cp.client.get(f"{base}/ide/", headers=owner.h)).status_code == 404


async def test_api_keys_can_drive_projects(cp):
    owner = await cp.signup("auto@example.com")
    key = await cp.api_key(owner)
    h = {"authorization": f"Bearer {key}"}
    r = await cp.client.post(f"/api/v1/orgs/{owner.org_id}/projects", json={"name": "Scripted"}, headers=h)
    assert r.status_code == 200
    project = r.json()
    await cp.ctx.sandboxes.wait_until_ready(project["id"], 10)
    r = await cp.client.put(f"/api/v1/projects/{project['id']}/sbx/files/content", json={"path": "a.txt", "content": "hi"}, headers=h)
    assert r.status_code == 200
    # ...but the browser IDE needs a person.
    assert (await cp.client.post(f"/api/v1/projects/{project['id']}/ide-ticket", headers=h)).status_code == 403


async def test_stopped_and_vanished_sandboxes(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    pid = project["id"]
    first_token = cp.project_token(pid)
    models = "/llm/v1/models"
    assert (await cp.client.get(models, headers={"authorization": f"Bearer {first_token}"})).status_code == 200

    r = await cp.client.post(f"/api/v1/projects/{pid}/sandbox/stop", headers=alice.h)
    assert r.json()["sandbox"]["status"] == "stopped"
    r = await cp.client.get(f"/api/v1/projects/{pid}/sbx/files/tree", headers=alice.h)
    assert r.status_code == 409 and r.json()["sandbox_status"] == "stopped"
    # Stopping revokes the sandbox's platform token immediately.
    assert (await cp.client.get(models, headers={"authorization": f"Bearer {first_token}"})).status_code == 401

    await cp.client.post(f"/api/v1/projects/{pid}/sandbox/start", headers=alice.h)
    await cp.ctx.sandboxes.wait_until_ready(pid, 10)
    second_token = cp.project_token(pid)
    assert second_token != first_token
    assert (await cp.client.get(models, headers={"authorization": f"Bearer {second_token}"})).status_code == 200

    # The sandbox disappears without the control plane noticing (node lost, OOM...).
    cp.driver.kill(pid)
    r = await cp.client.get(f"/api/v1/projects/{pid}/sbx/files/tree", headers=alice.h)
    assert r.status_code == 503
    # Starting again reconciles the stale "running" state instead of trusting it.
    r = await cp.client.post(f"/api/v1/projects/{pid}/sandbox/start", headers=alice.h)
    assert r.status_code == 200
    await cp.ctx.sandboxes.wait_until_ready(pid, 10)
    assert (await cp.client.get(f"/api/v1/projects/{pid}/sbx/files/tree", headers=alice.h)).status_code == 200
    assert (await cp.client.get(models, headers={"authorization": f"Bearer {second_token}"})).status_code == 401


async def test_reconcile_on_boot_marks_lost_sandboxes_stopped(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    cp.driver.kill(project["id"])
    assert await cp.ctx.sandboxes.reconcile_all() == 1
    async with cp.ctx.db.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.sandbox_status == "stopped" and row.sandbox_error == "sandbox exited unexpectedly"


async def test_start_failures_are_reported(cp):
    alice = await cp.signup("alice@example.com")
    cp.driver.fail_next_start = RuntimeError("image pull failed")
    r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/projects", json={"name": "Broken"}, headers=alice.h)
    assert r.status_code == 200
    assert r.json()["sandbox"]["status"] == "error"
    assert "image pull failed" in r.json()["sandbox"]["error"]


async def test_plan_limits(cp):
    alice = await cp.signup("alice@example.com")
    await cp.create_project(alice, "One")
    second = (await cp.client.post(f"/api/v1/orgs/{alice.org_id}/projects", json={"name": "Two"}, headers=alice.h)).json()
    # Free plan: one running sandbox; the project is still created.
    assert second["sandbox"]["status"] == "stopped" and "allows 1 running sandbox" in second["sandbox"]["error"]
    r = await cp.client.post(f"/api/v1/projects/{second['id']}/sandbox/start", headers=alice.h)
    assert r.status_code == 409
    r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/projects", json={"name": "Big", "sandbox_size": "large", "start": False}, headers=alice.h)
    assert r.status_code == 403
    await cp.create_project(alice, "Three", start=False)
    r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/projects", json={"name": "Four", "start": False}, headers=alice.h)
    assert r.status_code == 403 and "3 projects" in r.json()["detail"]


async def test_out_of_credit_orgs_cannot_start_sandboxes(cp):
    alice = await cp.signup("alice@example.com")
    async with cp.ctx.db.sessionmaker() as db:
        org = await db.get(Organization, alice.org_id)
        org.balance_micros = -2_000_000
        await db.commit()
    project = (await cp.client.post(f"/api/v1/orgs/{alice.org_id}/projects", json={"name": "Broke"}, headers=alice.h)).json()
    assert "Out of credits" in project["sandbox"]["error"]
    assert (await cp.client.post(f"/api/v1/projects/{project['id']}/sandbox/start", headers=alice.h)).status_code == 402


async def test_settings_update_and_restart(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    r = await cp.client.patch(f"/api/v1/projects/{project['id']}", json={"settings": {"sim_model": "claude-haiku-5-5", "embedding_dims": 512}}, headers=alice.h)
    assert r.status_code == 200 and r.json()["restart_required"] is True
    r = await cp.client.post(f"/api/v1/projects/{project['id']}/sandbox/restart", headers=alice.h)
    assert r.status_code == 200
    await cp.ctx.sandboxes.wait_until_ready(project["id"], 10)
    env = cp.driver.specs[-1].env
    assert env["SANDBOX_SIM_MODEL"] == "claude-haiku-5-5" and env["SANDBOX_EMBEDDING_DIMS"] == "512"
    # Clearing a setting falls back to the platform default.
    await cp.client.patch(f"/api/v1/projects/{project['id']}", json={"settings": {"sim_model": None}}, headers=alice.h)
    assert "sim_model" not in (await cp.client.get(f"/api/v1/projects/{project['id']}", headers=alice.h)).json()["settings"]


async def test_delete_project(cp):
    owner = await cp.signup("owner@example.com")
    member = await cp.signup("member@example.com")
    await cp.add_member(owner, member, "member")
    project = await cp.create_project(owner)
    token = cp.project_token(project["id"])
    assert (await cp.client.delete(f"/api/v1/projects/{project['id']}", headers=member.h)).status_code == 403
    assert (await cp.client.delete(f"/api/v1/projects/{project['id']}", headers=owner.h)).status_code == 200
    assert (await cp.client.get(f"/api/v1/projects/{project['id']}", headers=owner.h)).status_code == 404
    assert not (cp.driver.root / project["id"]).exists()
    assert (await cp.client.get("/llm/v1/models", headers={"authorization": f"Bearer {token}"})).status_code == 401
    listed = (await cp.client.get(f"/api/v1/orgs/{owner.org_id}/projects", headers=owner.h)).json()["projects"]
    assert listed == []


async def test_reaper_bills_compute_and_stops_idle_sandboxes(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    two_hours_ago = utcnow() - timedelta(hours=2)
    async with cp.ctx.db.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        row.sandbox_billed_until = two_hours_ago
        row.sandbox_last_activity_at = two_hours_ago
        await db.commit()
    cp.driver.apps[cp.driver.hosts[project["id"]]].state.activity.last = time.time() - 7200

    stats = await cp.ctx.sandboxes.reap_once()
    assert stats["billed"] == 1 and stats["stopped_idle"] == 1
    async with cp.ctx.db.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.sandbox_status == "stopped" and row.sandbox_error == "stopped: idle"
        event = (await db.execute(select(UsageEvent).where(UsageEvent.kind == "compute"))).scalar_one()
        assert 7190 <= event.quantity <= 7300
        assert abs(event.cost_micros - 120_000) < 500  # 2h x $0.06/h
        org = await db.get(Organization, alice.org_id)
        assert abs(org.balance_micros - (5_000_000 - event.cost_micros)) == 0


async def test_busy_sandboxes_are_not_idle_stopped(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    async with cp.ctx.db.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        row.sandbox_last_activity_at = utcnow() - timedelta(hours=2)
        await db.commit()
    # The sandbox reports recent activity (e.g. an experiment writing logs).
    stats = await cp.ctx.sandboxes.reap_once()
    assert stats["stopped_idle"] == 0


async def test_ide_ticket_cookie_flow(cp):
    owner = await cp.signup("owner@example.com")
    member = await cp.signup("member@example.com")
    await cp.add_member(owner, member, "member")
    project = await cp.create_project(owner)
    pid = project["id"]

    r = await cp.client.post(f"/api/v1/projects/{pid}/ide-ticket", headers=member.h)
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    assert url.path == f"/ide/{pid}/"
    ticket = parse_qs(url.query)["ticket"][0]

    # Without a session there is no IDE.
    cp.client.cookies = httpx.Cookies()
    assert (await cp.client.get(f"/ide/{pid}/")).status_code == 401
    # The ticket is exchanged for a path-scoped HttpOnly cookie.
    r = await cp.client.get(f"/ide/{pid}/", params={"ticket": ticket})
    assert r.status_code == 303 and r.headers["location"] == f"/ide/{pid}/"
    cookie = r.headers["set-cookie"].lower()
    assert f"ide_{pid}=" in cookie and f"path=/ide/{pid}/" in cookie and "httponly" in cookie
    # A ticket for one project does not open another.
    other = await cp.create_project(owner, "Other", start=False)
    assert (await cp.client.get(f"/ide/{other['id']}/", params={"ticket": ticket})).status_code == 401
    # With the cookie, requests reach the sandbox's IDE route (no code-server here: 503 from sandboxd).
    r = await cp.client.get(f"/ide/{pid}/")
    assert r.status_code in (502, 503)

    # Removing the member ends their IDE access (after the short membership cache).
    from controlplane.api import proxy

    await cp.client.delete(f"/api/v1/orgs/{owner.org_id}/members/{member.user_id}", headers=owner.h)
    proxy._endpoints.items.clear()
    assert (await cp.client.get(f"/ide/{pid}/")).status_code == 409


async def test_ide_ticket_respects_project_setting(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice, start=False, settings={"ide_enabled": False})
    assert (await cp.client.post(f"/api/v1/projects/{project['id']}/ide-ticket", headers=alice.h)).status_code == 409


async def test_project_usage_breakdown(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    token = cp.project_token(project["id"])
    r = await cp.client.post(
        "/llm/v1/chat/completions", json={"model": "mock", "messages": [{"role": "user", "content": "hi"}]}, headers={"authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    await cp.flush_usage()
    usage = (await cp.client.get(f"/api/v1/projects/{project['id']}/usage", headers=alice.h)).json()
    assert usage["breakdown"][0]["model"] == "mock" and usage["breakdown"][0]["requests"] == 1
