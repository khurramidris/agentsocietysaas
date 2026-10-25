"""Reverse proxy to the upstream agentsociety2 backend running in this sandbox.

The upstream backend (``python -m agentsociety2.backend.run``) is single-workspace:
every route takes a ``workspace_path`` that must equal ``WORKSPACE_PATH``. The web
app therefore never sends paths; this proxy pins ``workspace_path`` to the
sandbox workspace on every request (query string and JSON bodies), so all upstream
endpoints - modules, custom modules, agent skills, prefill params, experiment
info/artifacts/logs and replay - work unchanged.
"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ..security import AgentAuth

router = APIRouter(prefix="/as2", tags=["agentsociety2-backend"], dependencies=[AgentAuth])

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
STRIP_REQUEST = HOP_BY_HOP | {"authorization", "x-sandbox-secret", "cookie"}


def _pin_body(body: bytes, content_type: str, workspace: str) -> bytes:
    if not body or not content_type.startswith("application/json"):
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(data, dict):
        data["workspace_path"] = workspace
        return json.dumps(data).encode()
    return body


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str):
    settings = request.app.state.settings
    if not settings.enable_backend:
        raise HTTPException(503, "agentsociety2 backend disabled in this sandbox")
    client: httpx.AsyncClient = request.app.state.backend_client
    workspace = str(settings.workspace)
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "workspace_path"]
    params.append(("workspace_path", workspace))
    body = _pin_body(await request.body(), request.headers.get("content-type", ""), workspace)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}
    upstream = client.build_request(request.method, f"/{path}", params=params, content=body, headers=headers)
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.ConnectError:
        raise HTTPException(503, "agentsociety2 backend is not running yet") from None
    except httpx.TimeoutException:
        raise HTTPException(504, "agentsociety2 backend timed out") from None
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=out_headers,
        background=BackgroundTask(resp.aclose),
    )
