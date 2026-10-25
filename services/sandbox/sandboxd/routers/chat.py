"""AI Social Scientist chat: Claude Code running headless inside the workspace.

Upstream's AI Social Scientist is a coding agent (Claude Code or Codex) working in a
workspace that carries the AgentSociety skills under ``.claude/skills``. Here the
same Claude Code CLI runs non-interactively (``-p --output-format stream-json``)
for each user turn, resuming the previous conversation with ``--resume``. Its
Anthropic traffic goes to the platform LLM gateway (``ANTHROPIC_BASE_URL``) with
the project token, so usage is metered per organisation.

Each turn's events are appended to a JSONL log, so a browser can disconnect and
re-attach to a running turn without losing output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..security import AgentAuth
from ..workspace_env import child_env

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[AgentAuth])

PERMISSION_MODES = ("bypassPermissions", "acceptEdits", "default", "plan")

PLATFORM_PROMPT = (
    "You are the AI Social Scientist assistant of a hosted AgentSociety research workspace. "
    "The user talks to you through a web chat and cannot run terminal commands themselves; "
    "run the needed commands yourself and summarise results. Files you write appear in the "
    "project's Files and Experiments views. Follow CLAUDE.md and the workspace skills."
)


@dataclass
class SessionMeta:
    id: str
    title: str
    created_at: float
    updated_at: float
    claude_session_id: str | None = None
    model: str | None = None
    turns: int = 0
    status: str = "idle"
    last_error: str | None = None
    last_result: dict | None = None


@dataclass
class LiveSession:
    meta: SessionMeta
    dir: Path
    seq: int = 0
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    def save_meta(self) -> None:
        self.meta.updated_at = time.time()
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.meta), indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    def emit(self, kind: str, data) -> dict:
        self.seq += 1
        event = {"seq": self.seq, "ts": time.time(), "kind": kind, "data": data}
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    def read_events(self, after: int = 0, limit: int = 5000) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        with open(self.events_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("seq", 0) > after:
                    out.append(event)
                    if len(out) >= limit:
                        break
        return out


class ChatManager:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.root: Path = settings.chat_dir
        self.sessions: dict[str, LiveSession] = {}
        self._load()

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for meta_path in self.root.glob("*/meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = SessionMeta(**data)
            except (json.JSONDecodeError, TypeError):
                continue
            meta.status = "idle"  # nothing survives an agent restart
            live = LiveSession(meta=meta, dir=meta_path.parent)
            events = live.read_events(limit=10**9)
            live.seq = events[-1]["seq"] if events else 0
            self.sessions[meta.id] = live

    def active_turns(self) -> int:
        return sum(1 for s in self.sessions.values() if s.meta.status == "running")

    def create(self, title: str | None) -> LiveSession:
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        live = LiveSession(meta=SessionMeta(id=sid, title=title or "New research chat", created_at=now, updated_at=now), dir=self.root / sid)
        live.dir.mkdir(parents=True)
        live.save_meta()
        self.sessions[sid] = live
        return live

    def get(self, sid: str) -> LiveSession:
        live = self.sessions.get(sid)
        if live is None:
            raise HTTPException(404, "Chat session not found")
        return live

    def claude_env(self, model: str) -> dict[str, str]:
        s = self.settings
        env = child_env(s)
        env.update(
            {
                "ANTHROPIC_BASE_URL": s.anthropic_base_url,
                "ANTHROPIC_AUTH_TOKEN": s.platform_token,
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": s.chat_small_model,
                "ANTHROPIC_SMALL_FAST_MODEL": s.chat_small_model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_BUG_COMMAND": "1",
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
                # The whole container is the sandbox; allows bypassPermissions as any uid.
                "IS_SANDBOX": "1",
            }
        )
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    async def start_turn(self, live: LiveSession, content: str, model: str | None, permission_mode: str) -> dict:
        async with live.lock:
            if live.meta.status == "running":
                raise HTTPException(409, "A turn is already running in this session")
            if not self.settings.anthropic_base_url or not self.settings.platform_token:
                raise HTTPException(503, "Chat is not configured: missing gateway URL or project token")
            model = model or live.meta.model or self.settings.chat_model
            argv = [
                self.settings.claude_bin,
                "-p",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--permission-mode", permission_mode,
                "--model", model,
                "--append-system-prompt", PLATFORM_PROMPT,
            ]
            if live.meta.claude_session_id:
                argv += ["--resume", live.meta.claude_session_id]
            user_event = live.emit("user", {"content": content, "model": model})
            live.meta.status = "running"
            live.meta.model = model
            live.meta.last_error = None
            if live.meta.turns == 0 and live.meta.title == "New research chat":
                live.meta.title = content.strip().splitlines()[0][:80] or live.meta.title
            live.save_meta()
            try:
                live.process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self.settings.workspace),
                    env=self.claude_env(model),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    limit=16 * 1024 * 1024,
                )
            except OSError as exc:
                live.meta.status = "idle"
                live.meta.last_error = f"failed to start Claude Code: {exc}"
                live.save_meta()
                live.emit("error", {"message": live.meta.last_error})
                live.emit("turn_end", {"exit_code": None, "duration_s": 0})
                raise HTTPException(500, live.meta.last_error) from None
            live.task = asyncio.create_task(self._pump(live, content))
            return user_event

    async def _pump(self, live: LiveSession, content: str) -> None:
        proc = live.process
        assert proc is not None and proc.stdin and proc.stdout and proc.stderr
        started = time.monotonic()
        stderr_tail: list[str] = []

        async def read_stderr() -> None:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    stderr_tail.append(line)
                    del stderr_tail[:-50]

        stderr_task = asyncio.create_task(read_stderr())
        try:
            proc.stdin.write(content.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    live.emit("log", {"line": line})
                    continue
                if payload.get("type") == "system" and payload.get("subtype") == "init" and payload.get("session_id"):
                    live.meta.claude_session_id = payload["session_id"]
                    live.save_meta()
                if payload.get("type") == "result":
                    live.meta.last_result = {
                        k: payload.get(k)
                        for k in ("subtype", "is_error", "duration_ms", "num_turns", "total_cost_usd", "usage", "session_id")
                    }
                    if payload.get("session_id"):
                        live.meta.claude_session_id = payload["session_id"]
                live.emit("claude", payload)
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            exit_code = None
            raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stderr_task, timeout=2)
            exit_code = proc.returncode
            if exit_code not in (0, None) and not (live.meta.last_result or {}).get("subtype"):
                live.meta.last_error = "\n".join(stderr_tail[-10:]) or f"Claude Code exited with {exit_code}"
                live.emit("error", {"message": live.meta.last_error})
            live.meta.turns += 1
            live.meta.status = "idle"
            live.process = None
            live.save_meta()
            live.emit("turn_end", {"exit_code": exit_code, "duration_s": round(time.monotonic() - started, 2)})

    async def interrupt(self, live: LiveSession) -> bool:
        proc = live.process
        if proc is None or proc.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return True

    async def shutdown(self) -> None:
        for live in self.sessions.values():
            await self.interrupt(live)


def _manager(request: Request) -> ChatManager:
    return request.app.state.chat


class CreateSession(BaseModel):
    title: str | None = Field(None, max_length=200)


@router.post("/sessions")
async def create_session(request: Request, body: CreateSession | None = None):
    live = _manager(request).create((body or CreateSession()).title)
    return asdict(live.meta)


@router.get("/sessions")
async def list_sessions(request: Request):
    sessions = sorted(_manager(request).sessions.values(), key=lambda s: -s.meta.updated_at)
    return {"sessions": [asdict(s.meta) for s in sessions]}


@router.get("/sessions/{sid}")
async def get_session(request: Request, sid: str):
    live = _manager(request).get(sid)
    return {**asdict(live.meta), "last_seq": live.seq}


@router.get("/sessions/{sid}/events")
async def get_events(request: Request, sid: str, after: int = Query(0, ge=0), limit: int = Query(2000, ge=1, le=20000)):
    live = _manager(request).get(sid)
    return {"events": live.read_events(after, limit), "last_seq": live.seq, "status": live.meta.status}


class SendMessage(BaseModel):
    content: str = Field(..., min_length=1, max_length=400_000)
    model: str | None = Field(None, max_length=128)
    permission_mode: str = Field("bypassPermissions")


@router.post("/sessions/{sid}/messages")
async def send_message(request: Request, sid: str, body: SendMessage):
    if body.permission_mode not in PERMISSION_MODES:
        raise HTTPException(400, f"permission_mode must be one of {PERMISSION_MODES}")
    manager = _manager(request)
    live = manager.get(sid)
    event = await manager.start_turn(live, body.content, body.model, body.permission_mode)
    request.app.state.activity.touch()
    return {"accepted": True, "user_seq": event["seq"]}


@router.post("/sessions/{sid}/interrupt")
async def interrupt(request: Request, sid: str):
    manager = _manager(request)
    return {"interrupted": await manager.interrupt(manager.get(sid))}


@router.delete("/sessions/{sid}")
async def delete_session(request: Request, sid: str):
    manager = _manager(request)
    live = manager.get(sid)
    await manager.interrupt(live)
    for child in live.dir.glob("*"):
        child.unlink(missing_ok=True)
    live.dir.rmdir()
    manager.sessions.pop(sid, None)
    return {"deleted": sid}


@router.get("/sessions/{sid}/stream")
async def stream(request: Request, sid: str, after: int = Query(0, ge=0)):
    """SSE: backlog after ``after``, then live events until the running turn ends."""
    live = _manager(request).get(sid)
    queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    live.subscribers.add(queue)

    async def events():
        last = after
        try:
            for event in live.read_events(after, limit=10**9):
                last = event["seq"]
                yield f"id: {last}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if live.meta.status != "running":
                yield f"event: idle\ndata: {json.dumps({'last_seq': last})}\n\n"
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    if live.meta.status != "running":
                        yield f"event: idle\ndata: {json.dumps({'last_seq': last})}\n\n"
                        return
                    continue
                if event["seq"] <= last:
                    continue
                last = event["seq"]
                yield f"id: {last}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["kind"] == "turn_end":
                    yield f"event: idle\ndata: {json.dumps({'last_seq': last})}\n\n"
                    return
        finally:
            live.subscribers.discard(queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
