"""LLM gateway endpoints.

* ``/llm/v1/chat/completions``, ``/llm/v1/embeddings``, ``/llm/v1/models`` -
  OpenAI-compatible; what agentsociety2 (litellm ``openai/<model>``), the skills and
  customers' own code call.
* ``/llm/anthropic/v1/messages`` (+ ``count_tokens``, ``models``) - Anthropic
  Messages API; what Claude Code calls via ``ANTHROPIC_BASE_URL``.

Provider responses are relayed byte-for-byte (streams included) so client SDKs see
exactly what the provider sent; the gateway only swaps credentials, rewrites the
model name, reads token usage out of the response and meters it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from ..billing import plan_for
from ..models import ModelRoute, Organization, Provider
from . import mock
from .auth import Caller, CallerResolver, GatewayError, ModelResolver, ResolvedModel
from .metering import ConcurrencyLimiter, Usage, cost_micros

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("controlplane.gateway")
router = APIRouter(tags=["llm-gateway"])

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
PLAN_TTL_S = 30.0


def openai_error(err: GatewayError) -> JSONResponse:
    return JSONResponse({"error": {"message": err.message, "type": err.kind, "code": err.kind}}, status_code=err.status)


def anthropic_error(err: GatewayError) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": err.kind, "message": err.message}}, status_code=err.status)


class Gateway:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.callers = CallerResolver(ctx)
        self.models = ModelResolver(ctx)
        self.limiter = ConcurrencyLimiter()
        self._plans: dict[str, tuple[float, int]] = {}
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(ctx.settings.gateway_timeout_s, connect=10),
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=400),
        )

    async def close(self) -> None:
        await self.http.aclose()

    def invalidate(self) -> None:
        self.callers.invalidate()
        self.models.invalidate()

    async def _concurrency_limit(self, org_id: str) -> int:
        hit = self._plans.get(org_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with self.ctx.db.sessionmaker() as db:
            org = await db.get(Organization, org_id)
        limit = plan_for(org).max_llm_concurrency if org else 4
        self._plans[org_id] = (time.monotonic() + PLAN_TTL_S, limit)
        return limit

    async def preflight(self, request: Request, alias: str | None, kind: str) -> tuple[Caller, ResolvedModel, asyncio.Semaphore]:
        caller = await self.callers.resolve(request)
        resolved = await self.models.resolve(caller.org_id, alias or "", kind)
        if resolved.billable and (resolved.route.input_nanos or resolved.route.output_nanos):  # type: ignore[union-attr]
            if await self.ctx.usage.available_micros(caller.org_id) <= 0:
                raise GatewayError(402, "insufficient_credits", "Your organization is out of credits. Add credits to continue.")
        sem = self.limiter.get(caller.org_id, await self._concurrency_limit(caller.org_id))
        try:
            await asyncio.wait_for(sem.acquire(), timeout=300)
        except TimeoutError:
            raise GatewayError(429, "rate_limit_error", "Too many concurrent model requests for this organization") from None
        return caller, resolved, sem

    def record(self, caller: Caller, resolved: ResolvedModel, alias: str, kind: str, usage: Usage, status: int, started: float, request_id: str) -> None:
        cost = cost_micros(resolved.route, usage) if resolved.billable and status < 400 else 0
        self.ctx.usage.record(
            {
                "org_id": caller.org_id,
                "project_id": caller.project_id,
                "api_key_id": caller.api_key_id,
                "source": caller.source,
                "kind": kind,
                "model": alias,
                "provider_id": resolved.provider.id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
                "cost_micros": cost,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status_code": status,
                "request_id": request_id,
            }
        )

    def provider_key(self, provider: Provider) -> str:
        return self.ctx.encryptor.decrypt(provider.api_key_enc)


def _gw(request: Request) -> Gateway:
    return request.app.state.ctx.gateway


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GatewayError(400, "invalid_request_error", "Request body must be JSON") from None
    if not isinstance(body, dict):
        raise GatewayError(400, "invalid_request_error", "Request body must be a JSON object")
    return body


def _sse_data_lines(buffer: bytearray) -> list[bytes]:
    """Pop complete lines from ``buffer`` and return the ``data:`` payloads."""
    out = []
    while True:
        idx = buffer.find(b"\n")
        if idx < 0:
            return out
        line = bytes(buffer[:idx]).strip()
        del buffer[: idx + 1]
        if line.startswith(b"data:"):
            out.append(line[5:].strip())


async def _metered_stream(
    upstream: AsyncIterator[bytes],
    on_data: Callable[[dict], None],
    finish: Callable[[], None],
    cleanup: Callable[[], Any] | None = None,
) -> AsyncIterator[bytes]:
    buffer = bytearray()
    try:
        async for chunk in upstream:
            buffer.extend(chunk)
            for payload in _sse_data_lines(buffer):
                if payload and payload != b"[DONE]":
                    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                        obj = json.loads(payload)
                        if isinstance(obj, dict):
                            on_data(obj)
            yield chunk
    finally:
        finish()
        if cleanup is not None:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result


# --------------------------------------------------------------------- OpenAI


def _openai_headers(gw: Gateway, provider: Provider) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **{k: str(v) for k, v in (provider.extra_headers or {}).items()}}
    key = gw.provider_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def _litellm_call(gw: Gateway, resolved: ResolvedModel, body: dict[str, Any], *, embedding: bool = False):
    try:
        import litellm
    except ImportError:
        raise GatewayError(400, "invalid_request_error", "This model needs protocol translation, which is not installed") from None
    provider = resolved.provider
    model = resolved.upstream_model
    if provider.kind == "anthropic" and not model.startswith("anthropic/"):
        model = f"anthropic/{model}"
    kwargs = {k: v for k, v in body.items() if k not in ("model", "stream_options")}
    extra: dict[str, Any] = {}
    if provider.api_base:
        extra["api_base"] = provider.api_base
    key = gw.provider_key(provider)
    if key:
        extra["api_key"] = key
    if embedding:
        return await litellm.aembedding(model=model, **kwargs, **extra)
    if kwargs.get("stream"):
        kwargs["stream_options"] = {"include_usage": True}
    return await litellm.acompletion(model=model, **kwargs, **extra)


@router.post("/llm/v1/chat/completions")
async def chat_completions(request: Request):
    gw = _gw(request)
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    try:
        body = await _read_json(request)
        caller, resolved, sem = await gw.preflight(request, body.get("model"), "chat")
    except GatewayError as err:
        return openai_error(err)
    alias = str(body.get("model"))
    stream = bool(body.get("stream"))
    usage = Usage()
    handed_off = False
    headers = {"x-request-id": request_id}
    try:
        kind = resolved.provider.kind
        if kind == "mock":
            response, raw_usage = mock.openai_chat(body)
            gw.record(caller, resolved, alias, "llm", Usage.from_openai(raw_usage), 200, started, request_id)
            if stream:
                return StreamingResponse(mock.openai_chat_stream(response, raw_usage), media_type="text/event-stream", headers=headers)
            return JSONResponse(response, headers=headers)

        if kind == "openai":
            upstream_body = {**body, "model": resolved.upstream_model}
            if stream:
                upstream_body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
            url = resolved.provider.api_base.rstrip("/") + "/chat/completions"
            req = gw.http.build_request("POST", url, json=upstream_body, headers=_openai_headers(gw, resolved.provider))
            try:
                resp = await gw.http.send(req, stream=stream)
            except httpx.HTTPError as exc:
                gw.record(caller, resolved, alias, "llm", usage, 502, started, request_id)
                return openai_error(GatewayError(502, "upstream_error", f"Provider unreachable: {exc}"))
            if not stream:
                status = resp.status_code
                try:
                    data = resp.json()
                except ValueError:
                    gw.record(caller, resolved, alias, "llm", usage, 502, started, request_id)
                    return Response(resp.content, status_code=status, media_type=resp.headers.get("content-type"))
                if isinstance(data, dict):
                    usage = Usage.from_openai(data.get("usage"))
                    if "model" in data:
                        data["model"] = alias
                gw.record(caller, resolved, alias, "llm", usage, status, started, request_id)
                return JSONResponse(data, status_code=status, headers=headers)

            status = resp.status_code

            def on_data(obj: dict) -> None:
                nonlocal usage
                if obj.get("usage"):
                    usage = Usage.from_openai(obj["usage"])

            def finish() -> None:
                gw.record(caller, resolved, alias, "llm", usage, status, started, request_id)
                sem.release()

            handed_off = True
            return StreamingResponse(
                _metered_stream(resp.aiter_raw(), on_data, finish, resp.aclose),
                status_code=status,
                media_type=resp.headers.get("content-type", "text/event-stream"),
                headers=headers,
            )

        # anthropic / litellm: translate the OpenAI request to the provider protocol.
        result = await _litellm_call(gw, resolved, body)
        if not stream:
            data = result.model_dump()
            raw = data.get("usage") or {}
            usage = Usage.from_openai(raw)
            if raw.get("cache_read_input_tokens") or raw.get("cache_creation_input_tokens"):
                usage.merge_anthropic(raw)
            data["model"] = alias
            gw.record(caller, resolved, alias, "llm", usage, 200, started, request_id)
            return JSONResponse(data, headers=headers)

        async def translated() -> AsyncIterator[bytes]:
            nonlocal usage
            try:
                async for chunk in result:
                    payload = chunk.model_dump()
                    payload["model"] = alias
                    if payload.get("usage"):
                        usage = Usage.from_openai(payload["usage"])
                    yield f"data: {json.dumps(payload)}\n\n".encode()
                yield b"data: [DONE]\n\n"
            finally:
                gw.record(caller, resolved, alias, "llm", usage, 200, started, request_id)
                sem.release()

        handed_off = True
        return StreamingResponse(translated(), media_type="text/event-stream", headers=headers)
    except GatewayError as err:
        return openai_error(err)
    except Exception as exc:  # provider SDK errors from translation
        log.exception("gateway chat error")
        gw.record(caller, resolved, alias, "llm", usage, 502, started, request_id)
        status = getattr(exc, "status_code", 502) or 502
        return openai_error(GatewayError(int(status), "upstream_error", str(exc)[:500]))
    finally:
        if not handed_off:
            sem.release()


@router.post("/llm/v1/embeddings")
async def embeddings(request: Request):
    gw = _gw(request)
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    try:
        body = await _read_json(request)
        caller, resolved, sem = await gw.preflight(request, body.get("model"), "embedding")
    except GatewayError as err:
        return openai_error(err)
    alias = str(body.get("model"))
    try:
        kind = resolved.provider.kind
        if kind == "mock":
            data, raw = mock.embeddings(body, dims=gw.ctx.settings.default_embedding_dims)
            gw.record(caller, resolved, alias, "embedding", Usage(input_tokens=raw["prompt_tokens"]), 200, started, request_id)
            return JSONResponse(data)
        if kind == "openai":
            url = resolved.provider.api_base.rstrip("/") + "/embeddings"
            try:
                resp = await gw.http.post(url, json={**body, "model": resolved.upstream_model}, headers=_openai_headers(gw, resolved.provider))
            except httpx.HTTPError as exc:
                return openai_error(GatewayError(502, "upstream_error", f"Provider unreachable: {exc}"))
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            usage = Usage(input_tokens=int(((data or {}).get("usage") or {}).get("prompt_tokens") or 0))
            if isinstance(data, dict) and "model" in data:
                data["model"] = alias
            gw.record(caller, resolved, alias, "embedding", usage, resp.status_code, started, request_id)
            return JSONResponse(data, status_code=resp.status_code)
        result = await _litellm_call(gw, resolved, body, embedding=True)
        data = result.model_dump()
        usage = Usage(input_tokens=int((data.get("usage") or {}).get("prompt_tokens") or 0))
        gw.record(caller, resolved, alias, "embedding", usage, 200, started, request_id)
        return JSONResponse(data)
    except GatewayError as err:
        return openai_error(err)
    finally:
        sem.release()


async def _visible_models(request: Request, gw: Gateway, caller: Caller) -> list[dict]:
    async with gw.ctx.db.sessionmaker() as db:
        rows = (
            await db.execute(
                select(ModelRoute).where(
                    ModelRoute.enabled.is_(True),
                    (ModelRoute.org_id == caller.org_id) | ModelRoute.org_id.is_(None),
                )
            )
        ).scalars().all()
    return [{"id": r.alias, "object": "model", "owned_by": "organization" if r.org_id else "platform", "kind": r.kind} for r in rows]


@router.get("/llm/v1/models")
async def list_models(request: Request):
    gw = _gw(request)
    try:
        caller = await gw.callers.resolve(request)
    except GatewayError as err:
        return openai_error(err)
    return {"object": "list", "data": await _visible_models(request, gw, caller)}


# ------------------------------------------------------------------ Anthropic


def _anthropic_headers(gw: Gateway, request: Request, provider: Provider) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", DEFAULT_ANTHROPIC_VERSION),
        **{k: str(v) for k, v in (provider.extra_headers or {}).items()},
    }
    if request.headers.get("anthropic-beta"):
        headers["anthropic-beta"] = request.headers["anthropic-beta"]
    key = gw.provider_key(provider)
    if key:
        headers["x-api-key"] = key
    return headers


def _anthropic_url(provider: Provider, path: str, request: Request) -> str:
    base = (provider.api_base or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    query = request.url.query
    return f"{base}/v1/{path}" + (f"?{query}" if query else "")


@router.post("/llm/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    gw = _gw(request)
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    try:
        body = await _read_json(request)
        caller, resolved, sem = await gw.preflight(request, body.get("model"), "chat")
    except GatewayError as err:
        return anthropic_error(err)
    alias = str(body.get("model"))
    stream = bool(body.get("stream"))
    usage = Usage()
    handed_off = False
    headers = {"request-id": request_id}
    try:
        kind = resolved.provider.kind
        if kind == "mock":
            msg = mock.anthropic_message(body)
            usage.merge_anthropic(msg["usage"])
            gw.record(caller, resolved, alias, "llm", usage, 200, started, request_id)
            if stream:
                return StreamingResponse(mock.anthropic_message_stream(msg), media_type="text/event-stream", headers=headers)
            return JSONResponse(msg, headers=headers)
        if kind != "anthropic":
            raise GatewayError(400, "invalid_request_error", f"Model '{alias}' is not served over the Anthropic Messages API")
        upstream_body = {**body, "model": resolved.upstream_model}
        req = gw.http.build_request(
            "POST", _anthropic_url(resolved.provider, "messages", request), json=upstream_body,
            headers=_anthropic_headers(gw, request, resolved.provider),
        )
        try:
            resp = await gw.http.send(req, stream=stream)
        except httpx.HTTPError as exc:
            gw.record(caller, resolved, alias, "llm", usage, 502, started, request_id)
            return anthropic_error(GatewayError(502, "api_error", f"Provider unreachable: {exc}"))
        status = resp.status_code
        if not stream:
            try:
                data = resp.json()
            except ValueError:
                gw.record(caller, resolved, alias, "llm", usage, status, started, request_id)
                return Response(resp.content, status_code=status, media_type=resp.headers.get("content-type"))
            if isinstance(data, dict):
                usage.merge_anthropic(data.get("usage"))
                if "model" in data:
                    data["model"] = alias
            gw.record(caller, resolved, alias, "llm", usage, status, started, request_id)
            return JSONResponse(data, status_code=status, headers=headers)

        def on_data(obj: dict) -> None:
            if obj.get("type") == "message_start":
                usage.merge_anthropic((obj.get("message") or {}).get("usage"))
            elif obj.get("type") == "message_delta":
                usage.merge_anthropic(obj.get("usage"))

        def finish() -> None:
            gw.record(caller, resolved, alias, "llm", usage, status, started, request_id)
            sem.release()

        handed_off = True
        return StreamingResponse(
            _metered_stream(resp.aiter_raw(), on_data, finish, resp.aclose),
            status_code=status,
            media_type=resp.headers.get("content-type", "text/event-stream"),
            headers=headers,
        )
    except GatewayError as err:
        return anthropic_error(err)
    finally:
        if not handed_off:
            sem.release()


@router.post("/llm/anthropic/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    gw = _gw(request)
    try:
        body = await _read_json(request)
        caller = await gw.callers.resolve(request)
        resolved = await gw.models.resolve(caller.org_id, body.get("model") or "", "chat")
    except GatewayError as err:
        return anthropic_error(err)
    if resolved.provider.kind == "mock":
        return mock.anthropic_count_tokens(body)
    if resolved.provider.kind != "anthropic":
        return anthropic_error(GatewayError(400, "invalid_request_error", "count_tokens needs an Anthropic model"))
    try:
        resp = await gw.http.post(
            _anthropic_url(resolved.provider, "messages/count_tokens", request),
            json={**body, "model": resolved.upstream_model},
            headers=_anthropic_headers(gw, request, resolved.provider),
        )
    except httpx.HTTPError as exc:
        return anthropic_error(GatewayError(502, "api_error", f"Provider unreachable: {exc}"))
    return Response(resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))


@router.get("/llm/anthropic/v1/models")
async def anthropic_models(request: Request):
    gw = _gw(request)
    try:
        caller = await gw.callers.resolve(request)
    except GatewayError as err:
        return anthropic_error(err)
    models = [m for m in await _visible_models(request, gw, caller) if m["kind"] == "chat"]
    return {
        "data": [{"type": "model", "id": m["id"], "display_name": m["id"], "created_at": "2026-01-01T00:00:00Z"} for m in models],
        "has_more": False,
        "first_id": models[0]["id"] if models else None,
        "last_id": models[-1]["id"] if models else None,
    }
