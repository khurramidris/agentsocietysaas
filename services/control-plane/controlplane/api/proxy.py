"""Authenticated pass-through from browsers to each project's sandbox agent.

``/api/v1/projects/{id}/sbx/<path>`` forwards to ``<sandbox>/<path>`` (files,
experiments, tools, workspace, chat, and ``as2/...`` for the upstream backend).
GET-like requests need the viewer role, everything else member. Bodies and
responses are streamed in both directions, so uploads, SSE log/chat streams and
archive downloads work without buffering.

``/ide/{id}/...`` serves the browser IDE (code-server) on
``CP_IDE_BASE_URL``. Access starts with a one-time ticket from
``POST /api/v1/projects/{id}/ide-ticket`` that is exchanged for a path-scoped
session cookie; WebSocket upgrades must also come from the IDE origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import urlencode, urlparse

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from ..db import utcnow
from ..deps import Principal, ctx, get_db, get_principal, project_access
from ..models import Project, User
from ..security import make_ticket, read_ticket

router = APIRouter(tags=["sandbox-proxy"])

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
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
FORWARD_REQUEST_HEADERS = {"content-type", "accept", "accept-encoding", "last-event-id", "range", "if-none-match", "if-modified-since"}
STREAM_TIMEOUT = httpx.Timeout(None, connect=10)
IDE_SESSION_TTL_S = 8 * 3600


def _not_running(project: Project) -> JSONResponse:
    status = 503 if project.sandbox_status == "starting" else 409
    return JSONResponse(
        {"detail": f"Sandbox is {project.sandbox_status}", "sandbox_status": project.sandbox_status, "sandbox_error": project.sandbox_error},
        status_code=status,
        headers={"Retry-After": "3"} if status == 503 else {},
    )


async def forward(request: Request, project: Project, path: str, *, extra_query: list[tuple[str, str]] | None = None):
    manager = ctx(request).sandboxes
    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
    headers.update(manager.agent_headers(project))
    params = list(request.query_params.multi_items()) + (extra_query or [])
    body = request.stream() if request.method not in READ_METHODS else None
    upstream = manager.http.build_request(
        request.method, f"{project.sandbox_endpoint}/{path}", params=params, content=body, headers=headers, timeout=STREAM_TIMEOUT
    )
    try:
        resp = await manager.http.send(upstream, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"detail": "Sandbox is not reachable", "sandbox_status": "unreachable"}, status_code=503, headers={"Retry-After": "5"})
    out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out, background=BackgroundTask(resp.aclose))


# Pass-through routes stay out of the OpenAPI schema: the sandbox API documents itself at
# /api/v1/projects/{id}/sbx/openapi.json.
@router.api_route("/api/v1/projects/{project_id}/sbx/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def sandbox_proxy(project_id: str, path: str, request: Request, principal: Principal = Depends(get_principal), db=Depends(get_db)):
    min_role = "viewer" if request.method in READ_METHODS else "member"
    pa = await project_access(db, principal, project_id, min_role)
    project = pa.project
    if path == "ide" or path.startswith("ide/"):
        raise HTTPException(404, "Use the IDE endpoint")
    if project.sandbox_status != "running" or not project.sandbox_endpoint:
        return _not_running(project)
    await ctx(request).sandboxes.touch(db, project)
    return await forward(request, project, path)


# ------------------------------------------------------------------------ IDE


@router.post("/api/v1/projects/{project_id}/ide-ticket")
async def ide_ticket(project_id: str, request: Request, principal: Principal = Depends(get_principal), db=Depends(get_db)):
    pa = await project_access(db, principal, project_id, "member")
    if not (pa.project.settings or {}).get("ide_enabled", ctx(request).settings.sandbox_enable_ide):
        raise HTTPException(409, "The browser IDE is disabled for this project")
    settings = ctx(request).settings
    if principal.user is None:
        raise HTTPException(403, "The browser IDE requires a user session")
    ticket = make_ticket(settings, "ide-ticket", f"{project_id}:{principal.user.id}", 120)
    return {"url": f"{settings.effective_ide_base_url}/ide/{project_id}/?{urlencode({'ticket': ticket})}", "expires_in": 120}


class _EndpointCache:
    """(project, user) -> (expires, endpoint, secret).

    Avoids a database hit per IDE asset request while still re-checking, every
    few seconds, that the sandbox runs and that the user is still a member with
    at least the member role (so removing someone ends their IDE access quickly).
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], tuple[float, str, str]] = {}

    async def get(self, request_app, project_id: str, user_id: str) -> tuple[str, str] | None:
        key = (project_id, user_id)
        hit = self.items.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]
        self.items.pop(key, None)
        cp = request_app.state.ctx
        async with cp.db.sessionmaker() as db:
            project = await db.get(Project, project_id)
            if project is None or project.archived_at is not None:
                return None
            user = await db.get(User, user_id)
            if user is None or not user.is_active:
                return None
            try:
                await project_access(db, Principal(user=user), project_id, "member")
            except HTTPException:
                return None
            if project.sandbox_status != "running" or not project.sandbox_endpoint:
                return None
            project.sandbox_last_activity_at = utcnow()
            await db.commit()
            secret = cp.sandboxes.agent_secret(project)
            self.items[key] = (time.monotonic() + 10, project.sandbox_endpoint, secret)
            return project.sandbox_endpoint, secret


_endpoints = _EndpointCache()


def _cookie_name(project_id: str) -> str:
    return f"ide_{project_id}"


@router.api_route("/ide/{project_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def ide_http(project_id: str, path: str, request: Request):
    settings = ctx(request).settings
    ticket = request.query_params.get("ticket")
    if ticket:
        subject = read_ticket(settings, "ide-ticket", ticket)
        if subject is None or not subject.startswith(f"{project_id}:"):
            raise HTTPException(401, "IDE link expired; open the IDE again from the project page")
        session = make_ticket(settings, "ide-session", subject, IDE_SESSION_TTL_S)
        query = [(k, v) for k, v in request.query_params.multi_items() if k != "ticket"]
        target = f"/ide/{project_id}/{path}" + (f"?{urlencode(query)}" if query else "")
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            _cookie_name(project_id),
            session,
            max_age=IDE_SESSION_TTL_S,
            httponly=True,
            secure=settings.effective_ide_base_url.startswith("https://"),
            samesite="lax",
            path=f"/ide/{project_id}/",
        )
        return resp
    session = request.cookies.get(_cookie_name(project_id), "")
    subject = read_ticket(settings, "ide-session", session) if session else None
    if subject is None or not subject.startswith(f"{project_id}:"):
        raise HTTPException(401, "Not signed in to this IDE; open it from the project page")
    target = await _endpoints.get(request.app, project_id, subject.split(":", 1)[1])
    if target is None:
        raise HTTPException(409, "The sandbox is not running or you no longer have access")
    endpoint, secret = target
    client: httpx.AsyncClient = ctx(request).sandboxes.http
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP | {"cookie", "authorization", "origin"}}
    headers["Authorization"] = f"Bearer {secret}"
    upstream = client.build_request(
        request.method,
        f"{endpoint}/ide/{path}",
        params=list(request.query_params.multi_items()),
        content=request.stream() if request.method not in READ_METHODS else None,
        headers=headers,
        timeout=STREAM_TIMEOUT,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError:
        raise HTTPException(503, "IDE is starting") from None
    out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out, background=BackgroundTask(resp.aclose))


@router.websocket("/ide/{project_id}/{path:path}")
async def ide_ws(websocket: WebSocket, project_id: str, path: str):
    app = websocket.app
    settings = app.state.ctx.settings
    origin = websocket.headers.get("origin", "")
    allowed = urlparse(settings.effective_ide_base_url)
    if origin and urlparse(origin).netloc != allowed.netloc:
        await websocket.close(code=4403)
        return
    session = websocket.cookies.get(_cookie_name(project_id), "")
    subject = read_ticket(settings, "ide-session", session) if session else None
    if subject is None or not subject.startswith(f"{project_id}:"):
        await websocket.close(code=4401)
        return
    target = await _endpoints.get(app, project_id, subject.split(":", 1)[1])
    if target is None:
        await websocket.close(code=4409)
        return
    endpoint, secret = target
    ws_url = endpoint.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + f"/ide/{path}"
    if websocket.url.query:
        ws_url += f"?{websocket.url.query}"
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        upstream = await websockets.connect(
            ws_url, additional_headers={"x-sandbox-secret": secret}, subprotocols=protocols or None, max_size=None, open_timeout=20
        )
    except (OSError, websockets.InvalidHandshake):
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=upstream.subprotocol)

    async def downstream() -> None:
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

    async def upstream_pump() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        finally:
            await upstream.close()

    await asyncio.gather(downstream(), upstream_pump(), return_exceptions=True)
