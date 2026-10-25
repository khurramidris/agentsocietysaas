"""Proxy to code-server, which runs the full AI Social Scientist extension.

code-server listens on 127.0.0.1 inside the sandbox with ``--auth none``; the only
way in is through this agent, which the control plane reaches with the sandbox
secret after authenticating the user. The ``Origin`` header is dropped on the last
hop because code-server compares it with its own Host; the control plane performs
the origin check against the platform's public URL instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ..security import require_agent_secret
from .as2 import HOP_BY_HOP

router = APIRouter(prefix="/ide", tags=["ide"])

STRIP = HOP_BY_HOP | {"authorization", "x-sandbox-secret", "origin", "cookie"}


def _ide(request_app):
    if not request_app.state.settings.ide_enabled or request_app.state.ide is None:
        raise HTTPException(503, "The browser IDE is not enabled for this sandbox")
    return request_app.state.ide


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
async def ide_http(request: Request, path: str):
    require_agent_secret(request)
    _ide(request.app)
    client: httpx.AsyncClient = request.app.state.ide_client
    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP}
    upstream = client.build_request(
        request.method,
        f"/{path}",
        params=list(request.query_params.multi_items()),
        content=await request.body(),
        headers=headers,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.ConnectError:
        raise HTTPException(503, "code-server is starting") from None
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out_headers, background=BackgroundTask(resp.aclose))


@router.websocket("/{path:path}")
async def ide_ws(websocket: WebSocket, path: str):
    app = websocket.app
    secret = app.state.settings.agent_secret
    supplied = websocket.headers.get("x-sandbox-secret") or websocket.headers.get("authorization", "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied.encode(), secret.encode()) or not app.state.settings.ide_enabled:
        await websocket.close(code=4401)
        return
    query = websocket.url.query
    target = f"ws://127.0.0.1:{app.state.settings.ide_port}/{path}" + (f"?{query}" if query else "")
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        upstream = await websockets.connect(target, subprotocols=protocols or None, max_size=None, open_timeout=20)
    except (OSError, websockets.InvalidHandshake):
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except WebSocketDisconnect:
            pass
        finally:
            await upstream.close()

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)
