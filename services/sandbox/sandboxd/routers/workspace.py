"""Workspace-level operations: initialise, inspect pipeline state, settings, import."""

from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..bootstrap import bootstrap, read_marker, read_overrides, refresh_module_info, write_overrides
from ..security import AgentAuth
from ..workspace_env import managed_values, quote_env_value, read_user_env, write_workspace_env

router = APIRouter(prefix="/workspace", tags=["workspace"], dependencies=[AgentAuth])

# Keys a project owner may override from the platform UI. Endpoints and tokens
# stay platform-controlled so a project cannot point its sandbox elsewhere with
# platform credentials.
OVERRIDABLE_KEYS = frozenset(
    {
        "AGENTSOCIETY_LLM_MODEL",
        "AGENTSOCIETY_CODER_LLM_MODEL",
        "AGENTSOCIETY_EMBEDDING_MODEL",
        "AGENTSOCIETY_EMBEDDING_DIMS",
        "AGENTSOCIETY_LLM_THINKING",
        "AGENTSOCIETY_LLM_REASONING_EFFORT",
        "AGENTSOCIETY_LLM_EXTRA_BODY",
        "AGENTSOCIETY_CODER_LLM_THINKING",
        "AGENTSOCIETY_CODER_LLM_REASONING_EFFORT",
        "AGENTSOCIETY_CODER_LLM_EXTRA_BODY",
        "AGENTSOCIETY_BATCH_SIZE",
        "AGENTSOCIETY_LLM_RAY_MAX_WORKERS",
        "AGENTSOCIETY_LLM_RAY_CONCURRENCY",
        "AGENTSOCIETY_ENV_ACTOR_MAX_CONCURRENCY",
        "AGENTSOCIETY_LLM_REQUEST_TIMEOUT",
    }
)
SECRET_KEYS = frozenset(
    {
        "AGENTSOCIETY_LLM_API_KEY",
        "AGENTSOCIETY_CODER_LLM_API_KEY",
        "AGENTSOCIETY_EMBEDDING_API_KEY",
        "LITERATURE_SEARCH_API_KEY",
        "AGENTSOCIETY_DATASET_TOKEN",
    }
)
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _settings(request: Request):
    return request.app.state.settings


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


@router.get("/info")
async def info(request: Request):
    settings = _settings(request)
    ws = settings.workspace
    topic_path = ws / "TOPIC.md"
    index = _read_json(ws / "papers" / "literature_index.json") or {}
    hypotheses = [p for p in ws.glob("hypothesis_*") if p.is_dir()]
    experiments = [e for h in hypotheses for e in h.glob("experiment_*") if e.is_dir()]
    return {
        "workspace": str(ws),
        "initialized": bool(read_marker(settings).get("initialized_at")),
        "marker": read_marker(settings),
        "topic": topic_path.read_text(encoding="utf-8") if topic_path.exists() else None,
        "progress": _read_json(ws / ".agentsociety" / "progress.json"),
        "counts": {
            "papers": len(index.get("entries", [])) if isinstance(index, dict) else 0,
            "hypotheses": len(hypotheses),
            "experiments": len(experiments),
        },
        "versions": request.app.state.versions,
    }


class InitBody(BaseModel):
    topic: str | None = None
    force_sync: bool = False


@router.post("/init")
async def init(request: Request, body: InitBody | None = None):
    body = body or InitBody()
    settings = _settings(request)
    result = await asyncio.to_thread(bootstrap, settings, topic=body.topic, force_sync=body.force_sync)
    return result.as_dict()


@router.post("/module-info/refresh")
async def module_info(request: Request):
    return await asyncio.to_thread(refresh_module_info, _settings(request))


@router.get("/pipeline")
async def pipeline(request: Request):
    progress = _read_json(_settings(request).workspace / ".agentsociety" / "progress.json")
    if progress is None:
        raise HTTPException(404, "progress.json not found; initialise the workspace")
    return progress


class TopicBody(BaseModel):
    content: str = Field(..., max_length=200_000)


@router.put("/topic")
async def put_topic(request: Request, body: TopicBody):
    path = _settings(request).workspace / "TOPIC.md"
    path.write_text(body.content, encoding="utf-8")
    return {"saved": True}


def _mask(key: str, value: str) -> str:
    if key in SECRET_KEYS and value:
        return value[:6] + "…" if len(value) > 10 else "…"
    return value


@router.get("/settings")
async def get_settings(request: Request):
    settings = _settings(request)
    overrides = read_overrides(settings)
    managed = managed_values(settings)
    effective = {**managed, **{k: v for k, v in overrides.items() if k in OVERRIDABLE_KEYS}}
    return {
        "overrides": overrides,
        "overridable_keys": sorted(OVERRIDABLE_KEYS),
        "effective": {k: _mask(k, v) for k, v in effective.items()},
        "user_env_keys": sorted(read_user_env(settings)),
    }


class SettingsBody(BaseModel):
    overrides: dict[str, str] = Field(default_factory=dict)
    restart_backend: bool = True


@router.put("/settings")
async def put_settings(request: Request, body: SettingsBody):
    """Persist per-project overrides of the managed ``.env`` block and apply them."""
    settings = _settings(request)
    bad = [k for k in body.overrides if k not in OVERRIDABLE_KEYS]
    if bad:
        raise HTTPException(400, f"Not overridable: {bad}")
    clean = {k: v for k, v in body.overrides.items() if v != ""}
    write_overrides(settings, clean)
    write_workspace_env(settings, clean)
    if body.restart_backend and request.app.state.backend is not None:
        await request.app.state.backend.restart()
    return {"overrides": clean}


class UserEnvBody(BaseModel):
    values: dict[str, str]


@router.put("/user-env")
async def put_user_env(request: Request, body: UserEnvBody):
    """Replace the user section of ``.env`` (variables outside the managed block)."""
    settings = _settings(request)
    managed = set(managed_values(settings)) | OVERRIDABLE_KEYS
    for key in body.values:
        if not _KEY_RE.fullmatch(key) or key in managed:
            raise HTTPException(400, f"Invalid or platform-managed key: {key}")
    env_path = settings.workspace / ".env"
    lines = [f"{k}={quote_env_value(v)}" for k, v in sorted(body.values.items())]
    # Rewrite: managed block first, then exactly the user's variables.
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    write_workspace_env(settings, read_overrides(settings))
    return {"keys": sorted(body.values)}


def _safe_members(zf: zipfile.ZipFile, dest: Path):
    dest_real = Path(os.path.realpath(dest))
    for member in zf.infolist():
        name = member.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            continue
        if parts[0] in {".git", ".env"} or parts[-1] == ".env":
            continue
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            continue  # never materialise symlinks from an archive
        target = Path(os.path.realpath(dest_real.joinpath(*parts)))
        if target != dest_real and dest_real not in target.parents:
            continue
        yield member, target


def _import_zip(settings, archive: Path, overwrite: bool) -> dict:
    written, skipped = 0, 0
    with zipfile.ZipFile(archive) as zf:
        members = list(_safe_members(zf, settings.workspace))
        # Strip a single common top-level directory (exports from other tools).
        names = [m.filename for m, _ in members]
        prefix = ""
        if names and all("/" in n for n in names):
            first = names[0].split("/", 1)[0]
            if all(n.split("/", 1)[0] == first for n in names):
                prefix = first + "/"
        for member, target in members:
            rel = member.filename[len(prefix):] if prefix and member.filename.startswith(prefix) else member.filename
            if not rel or rel.endswith("/"):
                continue
            target = Path(os.path.realpath(settings.workspace / rel))
            ws_real = Path(os.path.realpath(settings.workspace))
            if ws_real not in target.parents:
                skipped += 1
                continue
            if target.exists() and not overwrite:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            written += 1
    return {"written": written, "skipped": skipped}


@router.post("/import")
async def import_archive(request: Request, archive: UploadFile = File(...), overwrite: bool = False):
    """Import a workspace share package (.zip). Secrets and VCS state are ignored."""
    settings = _settings(request)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.state_dir / "import.zip"
    size = 0
    with open(tmp, "wb") as out:
        while chunk := await archive.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, "Archive too large")
            out.write(chunk)
    try:
        result = await asyncio.to_thread(_import_zip, settings, tmp, overwrite)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a zip archive") from None
    finally:
        tmp.unlink(missing_ok=True)
    boot = await asyncio.to_thread(bootstrap, settings)
    return {**result, "bootstrap": boot.as_dict()}
