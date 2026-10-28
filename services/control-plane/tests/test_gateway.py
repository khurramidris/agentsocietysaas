"""LLM gateway: protocol pass-through, metering and pricing, credits, auth, BYOK, egress."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from controlplane.gateway import egress
from controlplane.gateway import router as gateway_router
from controlplane.gateway.catalog import seed_catalog, validate_catalog
from controlplane.gateway.metering import Usage, cost_micros
from controlplane.models import ModelRoute, Organization, Provider, UsageEvent

OPENAI_BASE = "https://api.openai.test/v1"
ANTHROPIC_BASE = "https://api.anthropic.test"


async def add_model(
    cp, *, kind: str, api_base: str, alias: str, upstream: str, route_kind: str = "chat", org_id: str | None = None, key: str = "provider-key", **prices
) -> None:
    async with cp.ctx.db.sessionmaker() as db:
        provider = Provider(org_id=org_id, name=f"{kind}-{alias}", kind=kind, api_base=api_base, api_key_enc=cp.ctx.encryptor.encrypt(key))
        db.add(provider)
        await db.flush()  # no relationship(): the provider row must exist before its route
        db.add(ModelRoute(org_id=org_id, alias=alias, provider_id=provider.id, upstream_model=upstream, kind=route_kind, **prices))
        await db.commit()
    cp.ctx.gateway.invalidate()


async def usage_rows(cp) -> list[UsageEvent]:
    await cp.flush_usage()
    async with cp.ctx.db.sessionmaker() as db:
        return list((await db.execute(select(UsageEvent).where(UsageEvent.kind != "compute").order_by(UsageEvent.id))).scalars())


async def balance(cp, org_id: str) -> int:
    async with cp.ctx.db.sessionmaker() as db:
        return (await db.get(Organization, org_id)).balance_micros


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
async def sandbox_token(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    return alice, project, cp.project_token(project["id"])


# ---------------------------------------------------------------- pricing


def test_cost_tiers_and_long_context():
    route = ModelRoute(
        alias="m",
        provider_id="p",
        upstream_model="m",
        input_nanos=4000,
        output_nanos=20000,
        cache_read_nanos=200,
        cache_write_nanos=5000,
        cache_write_1h_nanos=8000,
    )
    usage = Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=10_000, cache_write_5m_tokens=2000, cache_write_1h_tokens=1000)
    # 1000*4000 + 500*20000 + 10000*200 + 2000*5000 + 1000*8000 = 34_000_000 nanos = 34_000 micros
    assert cost_micros(route, usage) == 34_000
    haiku = ModelRoute(
        alias="h", provider_id="p", upstream_model="h", input_nanos=100, output_nanos=500, long_context_tokens=100_000, long_context_multiplier=5.0
    )
    # At the 100K threshold the base price applies: $0.10/MTok in + $0.50/MTok out = $0.0105.
    assert cost_micros(haiku, Usage(input_tokens=100_000, output_tokens=1000)) == 10_500
    # Above it, every token costs 5x.
    assert cost_micros(haiku, Usage(input_tokens=100_001, output_tokens=1000)) == math.ceil((100_001 * 100 + 1000 * 500) * 5 / 1000)
    assert cost_micros(None, usage) == 0


def test_usage_parsing():
    u = Usage.from_openai({"prompt_tokens": 1200, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 1000}})
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (200, 1000, 30)
    a = Usage()
    a.merge_anthropic(
        {
            "input_tokens": 12,
            "cache_read_input_tokens": 900,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60},
            "output_tokens": 1,
        }
    )
    a.merge_anthropic({"output_tokens": 77})
    assert (a.input_tokens, a.cache_read_tokens, a.cache_write_5m_tokens, a.cache_write_1h_tokens, a.output_tokens) == (12, 900, 40, 60, 77)
    assert a.prompt_total == 1012


async def test_seeded_claude_catalog_prices(cp):
    cp.settings.anthropic_api_key = "test-anthropic-key"
    async with cp.ctx.db.sessionmaker() as db:
        await seed_catalog(db, cp.settings, cp.ctx.encryptor)
        routes = {r.alias: r for r in (await db.execute(select(ModelRoute).where(ModelRoute.org_id.is_(None)))).scalars()}
    opus = routes["claude-opus-5-5"]
    assert (opus.input_nanos, opus.output_nanos, opus.cache_read_nanos, opus.cache_write_nanos, opus.cache_write_1h_nanos) == (4000, 20000, 200, 5000, 8000)
    haiku = routes["claude-haiku-5-5"]
    assert (haiku.input_nanos, haiku.output_nanos, haiku.long_context_tokens, haiku.long_context_multiplier) == (100, 500, 100_000, 5.0)
    assert {"claude-sonnet-5-5", "claude-fable-5-1", "mock", "mock-embedding"} <= set(routes)


def test_catalog_prices_are_required():
    example = json.loads((Path(__file__).resolve().parents[3] / "deploy" / "model_catalog.example.json").read_text())
    # The example ships without prices so that an operator has to fill them in.
    with pytest.raises(ValueError, match=r"'gpt-5\.5' needs input_usd_per_mtok, output_usd_per_mtok"):
        validate_catalog(example)
    priced = [{**m, "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0} for m in example["models"]]
    validate_catalog({**example, "models": priced})
    assert {m["alias"] for m in priced} >= {"gpt-5.5", "text-embedding-3-large"}
    with pytest.raises(ValueError, match="needs output_usd_per_mtok"):
        validate_catalog({"models": [{"alias": "x", "provider": "p", "input_usd_per_mtok": 1}]})
    with pytest.raises(ValueError, match="needs input_usd_per_mtok"):
        validate_catalog({"models": [{"alias": "e", "provider": "p", "kind": "embedding", "input_usd_per_mtok": None}]})
    validate_catalog({"models": [{"alias": "e", "provider": "p", "kind": "embedding", "input_usd_per_mtok": 0}]})


# ------------------------------------------------------------------- mock


async def test_mock_chat_and_embeddings_are_metered(cp, sandbox_token):
    _alice, project, token = sandbox_token
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "mock", "messages": [{"role": "user", "content": "hello"}]}, headers=bearer(token))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"].startswith("Mock response")
    r = await cp.client.post(
        "/llm/v1/chat/completions", json={"model": "mock", "stream": True, "messages": [{"role": "user", "content": "hello"}]}, headers=bearer(token)
    )
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    r = await cp.client.post("/llm/v1/embeddings", json={"model": "mock-embedding", "input": ["a", "b"], "dimensions": 64}, headers=bearer(token))
    vectors = r.json()["data"]
    assert len(vectors) == 2 and len(vectors[0]["embedding"]) == 64
    # agentsociety2's tool loop: the mock calls the terminal tool with schema-valid arguments.
    tools = [
        {
            "type": "function",
            "function": {"name": "finish", "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}},
        }
    ]
    r = await cp.client.post(
        "/llm/v1/chat/completions", json={"model": "mock", "tools": tools, "messages": [{"role": "user", "content": "go"}]}, headers=bearer(token)
    )
    call = r.json()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "finish" and "answer" in json.loads(call["arguments"])

    rows = await usage_rows(cp)
    assert [(e.kind, e.model, e.project_id, e.source) for e in rows] == [
        ("llm", "mock", project["id"], "sandbox"),
        ("llm", "mock", project["id"], "sandbox"),
        ("embedding", "mock-embedding", project["id"], "sandbox"),
        ("llm", "mock", project["id"], "sandbox"),
    ]
    assert all(e.cost_micros == 0 for e in rows)


async def test_mock_anthropic_tool_loop(cp, sandbox_token):
    _, _, token = sandbox_token
    body = {
        "model": "mock",
        "max_tokens": 1024,
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        # Claude Code appends mid-conversation system messages after the user turn.
        "messages": [{"role": "user", "content": [{"type": "text", "text": "list files [mock:bash] ls -la"}]}, {"role": "system", "content": "env"}],
    }
    r = await cp.client.post("/llm/anthropic/v1/messages", json=body, headers={"x-api-key": token})
    msg = r.json()
    assert msg["stop_reason"] == "tool_use" and msg["content"][1]["input"]["command"] == "ls -la"
    body["stream"] = True
    r = await cp.client.post("/llm/anthropic/v1/messages", json=body, headers={"x-api-key": token})
    events = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event: ")]
    assert events[0] == "message_start" and events[-1] == "message_stop" and "content_block_delta" in events
    r = await cp.client.post("/llm/anthropic/v1/messages/count_tokens", json=body, headers={"x-api-key": token})
    assert r.json()["input_tokens"] > 0


# -------------------------------------------------------------- pass-through


async def test_openai_pass_through_is_faithful_and_billed(cp, sandbox_token):
    alice, _, token = sandbox_token
    await add_model(
        cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-test", upstream="gpt-upstream-2026", input_nanos=1000, output_nanos=4000, cache_read_nanos=100
    )
    upstream = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-upstream-2026",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 50, "total_tokens": 1250, "prompt_tokens_details": {"cached_tokens": 1000}},
    }
    before = await balance(cp, alice.org_id)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(200, json=upstream))
        r = await cp.client.post(
            "/llm/v1/chat/completions", json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}, headers=bearer(token)
        )
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-test"  # clients see the alias they asked for
    sent = route.calls.last.request
    assert json.loads(sent.content) == {"model": "gpt-upstream-2026", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    assert sent.headers["authorization"] == "Bearer provider-key"  # sandbox token never forwarded
    [event] = await usage_rows(cp)
    assert (event.input_tokens, event.cached_tokens, event.output_tokens) == (200, 1000, 50)
    assert event.cost_micros == 500  # (200*1000 + 1000*100 + 50*4000) / 1000
    assert await balance(cp, alice.org_id) == before - 500


async def test_openai_stream_is_relayed_byte_for_byte(cp, sandbox_token):
    _, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-test", upstream="gpt-up", input_nanos=1000, output_nanos=1000)
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "Hel"}}]},
        {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"id": "c", "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ]
    raw = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
    with respx.mock() as mock:
        route = mock.post(f"{OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw))
        r = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-test", "stream": True, "messages": []}, headers=bearer(token))
    assert r.content == raw
    assert json.loads(route.calls.last.request.content)["stream_options"] == {"include_usage": True}
    [event] = await usage_rows(cp)
    assert (event.input_tokens, event.output_tokens, event.cost_micros) == (10, 2, 12)


async def test_anthropic_pass_through_stream_and_cache_pricing(cp, sandbox_token):
    _alice, _, token = sandbox_token
    await add_model(
        cp,
        kind="anthropic",
        api_base=ANTHROPIC_BASE,
        alias="claude-opus-5-5",
        upstream="claude-opus-5-5",
        input_nanos=4000,
        output_nanos=20000,
        cache_read_nanos=200,
        cache_write_nanos=5000,
        cache_write_1h_nanos=8000,
    )
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "content": [],
                    "usage": {
                        "input_tokens": 50,
                        "cache_read_input_tokens": 1000,
                        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
                        "output_tokens": 1,
                    },
                },
            },
        ),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 30}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    raw = b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)
    with respx.mock() as mock:
        route = mock.post(f"{ANTHROPIC_BASE}/v1/messages").mock(return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw))
        r = await cp.client.post(
            "/llm/anthropic/v1/messages?beta=true",
            json={"model": "claude-opus-5-5", "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "some-beta-2026-01-01"},
        )
    assert r.content == raw
    sent = route.calls.last.request
    assert sent.url.query == b"beta=true"
    assert sent.headers["x-api-key"] == "provider-key" and "authorization" not in sent.headers
    assert sent.headers["anthropic-beta"] == "some-beta-2026-01-01" and sent.headers["anthropic-version"] == "2023-06-01"
    [event] = await usage_rows(cp)
    assert (event.input_tokens, event.cached_tokens, event.cache_write_tokens, event.output_tokens) == (50, 1000, 100, 30)
    # 50*4000 + 1000*200 + 100*5000 + 30*20000 = 1_500_000 nanos
    assert event.cost_micros == 1500


async def test_anthropic_non_stream_rewrites_model_and_relays_errors(cp, sandbox_token):
    _, _, token = sandbox_token
    await add_model(
        cp, kind="anthropic", api_base=ANTHROPIC_BASE, alias="claude-sonnet-5-5", upstream="claude-sonnet-5-5-20260901", input_nanos=2000, output_nanos=10000
    )
    ok = {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5-20260901",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    with respx.mock() as mock:
        mock.post(f"{ANTHROPIC_BASE}/v1/messages").mock(side_effect=[httpx.Response(200, json=ok), httpx.Response(529, json=overloaded)])
        body = {"model": "claude-sonnet-5-5", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
        first = await cp.client.post("/llm/anthropic/v1/messages", json=body, headers={"x-api-key": token})
        second = await cp.client.post("/llm/anthropic/v1/messages", json=body, headers={"x-api-key": token})
    assert first.json()["model"] == "claude-sonnet-5-5"
    assert second.status_code == 529 and second.json() == overloaded
    rows = await usage_rows(cp)
    assert [e.status_code for e in rows] == [200, 529] and rows[1].cost_micros == 0


async def test_unreachable_provider_is_a_clean_502(cp, sandbox_token):
    _, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-down", upstream="x", input_nanos=1)
    with respx.mock() as mock:
        mock.post(f"{OPENAI_BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
        r = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-down", "messages": []}, headers=bearer(token))
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


# --------------------------------------------------------- credits, auth


async def test_out_of_credits_blocks_billable_models_only(cp, sandbox_token):
    alice, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-test", upstream="x", input_nanos=1000)
    async with cp.ctx.db.sessionmaker() as db:
        (await db.get(Organization, alice.org_id)).balance_micros = 0
        await db.commit()
    cp.ctx.usage._balance_cache.clear()
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-test", "messages": []}, headers=bearer(token))
    assert r.status_code == 402 and r.json()["error"]["type"] == "insufficient_credits"
    r = await cp.client.post("/llm/anthropic/v1/messages", json={"model": "gpt-test", "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 402 and r.json()["type"] == "error"
    # Free models keep working.
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "mock", "messages": []}, headers=bearer(token))
    assert r.status_code == 200


async def test_unflushed_usage_counts_against_the_balance(cp, sandbox_token):
    alice, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-pricey", upstream="x", input_nanos=1_000_000_000)  # $1000/MTok... per token $1
    reply = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}], "usage": {"prompt_tokens": 6, "completion_tokens": 0}}
    with respx.mock() as mock:
        mock.post(f"{OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(200, json=reply))
        first = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-pricey", "messages": []}, headers=bearer(token))
        # $6 spent but not yet flushed; the $5 balance is exhausted for the next request.
        second = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-pricey", "messages": []}, headers=bearer(token))
    assert first.status_code == 200 and second.status_code == 402
    await cp.flush_usage()
    assert await balance(cp, alice.org_id) == 5_000_000 - 6_000_000


async def test_gateway_auth(cp, sandbox_token):
    alice, _, _ = sandbox_token
    body = {"model": "mock", "messages": []}
    assert (await cp.client.post("/llm/v1/chat/completions", json=body)).status_code == 401
    assert (await cp.client.post("/llm/v1/chat/completions", json=body, headers=bearer("sk_abc_def"))).status_code == 401
    assert (await cp.client.post("/llm/v1/chat/completions", json=body, headers=bearer(alice.token))).status_code == 401  # user JWTs are not API keys
    viewer_key = await cp.api_key(alice, role="viewer")
    r = await cp.client.post("/llm/v1/chat/completions", json=body, headers=bearer(viewer_key))
    assert r.status_code == 403
    member_key = await cp.api_key(alice, role="member")
    r = await cp.client.post("/llm/v1/chat/completions", json=body, headers={"api-key": member_key})
    assert r.status_code == 200
    [event] = await usage_rows(cp)
    assert event.source == "api" and event.api_key_id and event.project_id is None
    assert (await cp.client.post("/llm/v1/chat/completions", content=b"not json", headers=bearer(member_key))).status_code == 400


@pytest.mark.parametrize("cp", [{"enable_mock_provider": False}], indirect=True)
async def test_unknown_models_without_mock(cp, sandbox_token):
    _, _, token = sandbox_token
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "nope", "messages": []}, headers=bearer(token))
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"


async def test_model_kind_mismatch(cp, sandbox_token):
    _, _, token = sandbox_token
    r = await cp.client.post("/llm/v1/embeddings", json={"model": "mock", "input": "x"}, headers=bearer(token))
    assert r.status_code == 400


async def test_concurrency_limit(cp, sandbox_token, monkeypatch):
    alice, _, token = sandbox_token
    monkeypatch.setattr(gateway_router, "QUEUE_TIMEOUT_S", 0.05)
    sem = cp.ctx.gateway.limiter.get(alice.org_id, 8)  # free plan: 8 in flight
    for _ in range(8):
        await sem.acquire()
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "mock", "messages": []}, headers=bearer(token))
    assert r.status_code == 429
    sem.release()
    r = await cp.client.post("/llm/v1/chat/completions", json={"model": "mock", "messages": []}, headers=bearer(token))
    assert r.status_code == 200


async def test_model_listing(cp, sandbox_token):
    alice, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="org-private", upstream="x", org_id=alice.org_id)
    other = await cp.signup("other@example.com")
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="someone-elses", upstream="x", org_id=other.org_id)
    ids = {m["id"] for m in (await cp.client.get("/llm/v1/models", headers=bearer(token))).json()["data"]}
    assert {"mock", "mock-embedding", "org-private"} <= ids and "someone-elses" not in ids
    claude_ids = {m["id"] for m in (await cp.client.get("/llm/anthropic/v1/models", headers={"x-api-key": token})).json()["data"]}
    assert "mock" in claude_ids and "mock-embedding" not in claude_ids


# -------------------------------------------------------------------- BYOK


async def test_byok_routes_take_precedence_and_are_not_billed(cp, sandbox_token, monkeypatch):
    alice, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base=OPENAI_BASE, alias="gpt-shared", upstream="platform-model", input_nanos=1000)

    async def public_dns(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress, "resolve", public_dns)
    r = await cp.client.post(
        f"/api/v1/orgs/{alice.org_id}/providers",
        json={
            "name": "my-openai",
            "kind": "openai",
            "api_base": "https://llm.byok.example/v1",
            "api_key": "byok-secret",
            "extra_headers": {"Host": "evil", "X-Team": "lab"},
        },
        headers=alice.h,
    )
    assert r.status_code == 200 and r.json()["has_api_key"] is True
    provider_id = r.json()["id"]
    r = await cp.client.post(
        f"/api/v1/orgs/{alice.org_id}/models",
        json={"alias": "gpt-shared", "provider_id": provider_id, "upstream_model": "byok-model", "input_usd_per_mtok": 99},
        headers=alice.h,
    )
    assert r.status_code == 200 and r.json()["pricing_usd_per_mtok"]["input"] == 0  # BYOK is never charged

    reply = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 1}}
    with respx.mock() as mock:
        route = mock.post("https://93.184.216.34/v1/chat/completions").mock(return_value=httpx.Response(200, json=reply))
        r = await cp.client.post("/llm/v1/chat/completions", json={"model": "gpt-shared", "messages": []}, headers=bearer(token))
    assert r.status_code == 200
    sent = route.calls.last.request
    assert json.loads(sent.content)["model"] == "byok-model"
    assert sent.headers["host"] == "llm.byok.example" and sent.extensions["sni_hostname"] == "llm.byok.example"
    assert sent.headers["authorization"] == "Bearer byok-secret" and sent.headers["x-team"] == "lab"
    [event] = await usage_rows(cp)
    assert event.cost_micros == 0
    # Secrets are never returned.
    listed = (await cp.client.get(f"/api/v1/orgs/{alice.org_id}/models", headers=alice.h)).json()
    assert all("api_key" not in p for p in listed["providers"])


@pytest.mark.parametrize(
    "api_base",
    [
        "http://llm.example/v1",
        "https://169.254.169.254/latest",
        "https://10.0.0.5/v1",
        "https://[::1]/v1",
        "https://localhost/v1",
        "https://user:pw@llm.example/v1",
        "ftp://llm.example",
    ],
)
async def test_byok_rejects_unsafe_urls(cp, api_base):
    alice = await cp.signup("alice@example.com")
    r = await cp.client.post(f"/api/v1/orgs/{alice.org_id}/providers", json={"name": "x", "kind": "openai", "api_base": api_base}, headers=alice.h)
    assert r.status_code == 400, api_base


async def test_byok_dns_rebinding_to_private_addresses_is_blocked(cp, sandbox_token, monkeypatch):
    alice, _, token = sandbox_token
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])

    async def rebinding_dns(host, port):
        return next(answers)

    monkeypatch.setattr(egress, "resolve", rebinding_dns)
    r = await cp.client.post(
        f"/api/v1/orgs/{alice.org_id}/providers", json={"name": "x", "kind": "openai", "api_base": "https://rebind.example/v1"}, headers=alice.h
    )
    provider_id = r.json()["id"]
    await cp.client.post(f"/api/v1/orgs/{alice.org_id}/models", json={"alias": "rebind", "provider_id": provider_id, "upstream_model": "m"}, headers=alice.h)
    with respx.mock(assert_all_called=False) as mock:
        metadata = mock.route(host="169.254.169.254").mock(return_value=httpx.Response(200, text="iam-credentials"))
        public = mock.route(host="93.184.216.34").mock(return_value=httpx.Response(200, json={"choices": []}))
        first = await cp.client.post("/llm/v1/chat/completions", json={"model": "rebind", "messages": []}, headers=bearer(token))
        second = await cp.client.post("/llm/v1/chat/completions", json={"model": "rebind", "messages": []}, headers=bearer(token))
    assert public.call_count == 1
    assert first.status_code == 200  # first resolution: public, pinned
    assert second.status_code == 403 and not metadata.called


async def test_platform_providers_may_be_internal(cp, sandbox_token):
    """Operators can run models on private infrastructure (e.g. vLLM inside the VPC)."""
    _, _, token = sandbox_token
    await add_model(cp, kind="openai", api_base="http://10.1.2.3:8000/v1", alias="vllm", upstream="qwen", input_nanos=0)
    with respx.mock() as mock:
        mock.post("http://10.1.2.3:8000/v1/chat/completions").mock(return_value=httpx.Response(200, json={"choices": [], "usage": {}}))
        r = await cp.client.post("/llm/v1/chat/completions", json={"model": "vllm", "messages": []}, headers=bearer(token))
    assert r.status_code == 200


def test_egress_helpers():
    assert egress.is_public_ip("93.184.216.34")
    for private in (
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "::ffff:10.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ):
        assert not egress.is_public_ip(private), private
    assert egress.clean_extra_headers({"Host": "x", "content-length": "1", "X-A": 1}) == {"X-A": "1"}
    assert egress.validate_provider_url("https://api.example.com/v1/", allow_private=False) == "https://api.example.com/v1"
    assert egress.validate_provider_url("http://10.0.0.1:8000/v1", allow_private=True) == "http://10.0.0.1:8000/v1"


async def test_admin_catalog_api(cp):
    from controlplane.models import User

    admin = await cp.signup("ops@example.com")
    async with cp.ctx.db.sessionmaker() as db:
        (await db.get(User, admin.user_id)).is_platform_admin = True
        await db.commit()
    r = await cp.client.post("/api/v1/admin/providers", json={"name": "openai", "kind": "openai", "api_base": OPENAI_BASE, "api_key": "k"}, headers=admin.h)
    provider_id = r.json()["id"]
    r = await cp.client.post(
        "/api/v1/admin/models",
        json={"alias": "gpt-5.5", "provider_id": provider_id, "upstream_model": "gpt-5.5", "input_usd_per_mtok": 1.25, "output_usd_per_mtok": 10},
        headers=admin.h,
    )
    model = r.json()
    assert model["pricing_usd_per_mtok"]["input"] == 1.25 and model["pricing_usd_per_mtok"]["output"] == 10
    r = await cp.client.patch(f"/api/v1/admin/models/{model['id']}", json={"enabled": False}, headers=admin.h)
    assert r.json()["enabled"] is False
    member = await cp.signup("m@example.com")
    assert (await cp.client.get("/api/v1/admin/models", headers=member.h)).status_code == 403


async def test_usage_is_requeued_when_a_flush_fails(cp, sandbox_token, monkeypatch):
    _, _, token = sandbox_token
    await cp.client.post("/llm/v1/chat/completions", json={"model": "mock", "messages": []}, headers=bearer(token))
    recorder = cp.ctx.usage
    original = recorder.ctx.db.sessionmaker

    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(recorder.ctx.db, "sessionmaker", broken)
    assert await recorder.flush() == 0
    monkeypatch.setattr(recorder.ctx.db, "sessionmaker", original)
    assert len(await usage_rows(cp)) == 1


async def test_parallel_requests_share_one_flush(cp, sandbox_token):
    _, _, token = sandbox_token
    body = {"model": "mock", "messages": [{"role": "user", "content": "x"}]}
    results = await asyncio.gather(*[cp.client.post("/llm/v1/chat/completions", json=body, headers=bearer(token)) for _ in range(20)])
    assert all(r.status_code == 200 for r in results)
    assert await cp.flush_usage() == 20
