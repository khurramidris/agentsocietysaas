"""Migrations, security primitives, production guard rails and the web app host."""

from __future__ import annotations

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from controlplane import migrate
from controlplane.app import create_app
from controlplane.db import Base
from controlplane.security import Encryptor, make_ticket, new_composite, parse_composite, read_ticket
from controlplane.settings import Settings

from .conftest import Lifespan, make_settings


def test_migrations_match_the_models(tmp_path):
    db = tmp_path / "fresh.db"
    migrate.upgrade(f"sqlite+aiosqlite:///{db}")
    migrate.upgrade(f"sqlite+aiosqlite:///{db}")  # idempotent
    with create_engine(f"sqlite:///{db}").connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], "models changed without a migration: run controlplane.migrate.autogenerate"


def test_composite_tokens():
    token = new_composite("sbx")
    parsed = parse_composite(token.value)
    assert parsed == token and token.value.startswith("sbx_")
    for bad in ("", "sbx_only", "xx_ab_cd", "sk_zz_cd", "sk_ab_cd_ef", "sk__cd"):
        assert parse_composite(bad) is None, bad


def test_tickets_are_purpose_bound_and_expire():
    s = Settings(_env_file=None, secret_key="k" * 40)
    ticket = make_ticket(s, "ide-ticket", "prj_1:usr_1", 60)
    assert read_ticket(s, "ide-ticket", ticket) == "prj_1:usr_1"
    assert read_ticket(s, "ide-session", ticket) is None  # cannot be replayed for another purpose
    assert read_ticket(Settings(_env_file=None, secret_key="x" * 40), "ide-ticket", ticket) is None
    assert read_ticket(s, "ide-ticket", ticket[:-1] + ("0" if ticket[-1] != "0" else "1")) is None
    expired = make_ticket(s, "ide-ticket", "prj_1:usr_1", -1)
    assert read_ticket(s, "ide-ticket", expired) is None
    assert read_ticket(s, "ide-ticket", "garbage") is None


def test_encryptor_round_trip_and_key_separation():
    a = Encryptor(Settings(_env_file=None, secret_key="a" * 40))
    b = Encryptor(Settings(_env_file=None, secret_key="b" * 40))
    token = a.encrypt("provider-secret")
    assert "provider-secret" not in token and a.decrypt(token) == "provider-secret"
    assert b.decrypt(token) == "" and a.decrypt(None) == ""


def test_production_guard_rails():
    from cryptography.fernet import Fernet

    problems = Settings(_env_file=None, environment="production").validate_for_production()
    assert len(problems) == 4  # dev secret, no encryption key, local driver, SQLite
    assert len(Settings(_env_file=None, environment="production", enable_mock_provider=True).validate_for_production()) == 5
    good = Settings(
        _env_file=None,
        environment="production",
        secret_key="s" * 48,
        encryption_key=Fernet.generate_key().decode(),
        sandbox_driver="kubernetes",
        database_url="postgresql+asyncpg://cp@db/cp",
    )
    assert good.validate_for_production() == []
    with pytest.raises(RuntimeError, match="Refusing to start in production"):
        create_app(Settings(_env_file=None, environment="production"))


async def test_spa_is_served_with_security_headers(tmp_path, template_db, driver):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = create_app(make_settings(tmp_path, template_db, web_dist_dir=str(dist)), sandbox_driver=driver)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        r = await client.get("/projects/prj_1/files")  # client-side route
        assert r.status_code == 200 and "<title>app</title>" in r.text and r.headers["cache-control"] == "no-cache"
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        r = await client.get("/assets/app.js")
        assert r.headers["cache-control"].endswith("immutable")
        assert (await client.get("/api/v1/nope")).status_code == 404
        assert (await client.get("/../../etc/passwd")).status_code in (200, 404)  # never outside dist
        r = await client.get("/assets/../../cp.db")
        assert "SQLite" not in r.text


async def test_openapi_documents_the_platform(cp):
    spec = (await cp.client.get("/api/openapi.json")).json()
    paths = set(spec["paths"])
    for path in ("/api/v1/auth/signup", "/api/v1/orgs/{org_id}/projects", "/llm/v1/chat/completions", "/llm/anthropic/v1/messages", "/api/v1/data/datasets"):
        assert path in paths
    assert spec["info"]["title"] == "Societas API"


async def test_sandbox_api_documents_itself_through_the_proxy(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    spec = (await cp.client.get(f"/api/v1/projects/{project['id']}/sbx/openapi.json", headers=alice.h)).json()
    assert spec["info"]["title"] == "sandboxd" and "/files/tree" in spec["paths"]
