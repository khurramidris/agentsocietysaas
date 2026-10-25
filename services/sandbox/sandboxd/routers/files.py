"""Workspace file API: browse, read, edit, upload, move, delete and export."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..security import (
    AgentAuth,
    ensure_not_protected,
    is_protected,
    normalize_relative,
    resolve_in_workspace,
)

router = APIRouter(prefix="/files", tags=["files"], dependencies=[AgentAuth])

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache"}
# Never included in an exported archive (secrets and machine-local state), as upstream.
EXPORT_EXCLUDES = {".env", ".git", ".vscode", ".idea", "__pycache__", "node_modules", ".venv"}
MAX_DIR_ENTRIES = 1000


def _settings(request: Request):
    return request.app.state.settings


def _list_dir(root: Path, directory: Path, depth: int, show_hidden: bool) -> tuple[list[dict], bool]:
    entries: list[dict] = []
    truncated = False
    try:
        children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return entries, False
    for child in children:
        if child.name in SKIP_DIRS and child.is_dir():
            continue
        if not show_hidden and child.name.startswith("."):
            continue
        if len(entries) >= MAX_DIR_ENTRIES:
            truncated = True
            break
        item = _entry_safe(root, child)
        if item is None:
            continue
        if item["type"] == "directory" and depth > 1 and not item["symlink"]:
            item["children"], item["truncated"] = _list_dir(root, child, depth - 1, show_hidden)
        entries.append(item)
    return entries, truncated


def _entry_safe(root: Path, child: Path) -> dict | None:
    """Entry for ``child`` or None when it is a symlink escaping the workspace."""
    try:
        real = Path(os.path.realpath(child))
        root_real = Path(os.path.realpath(root))
        if real != root_real and root_real not in real.parents:
            return None
        parent_rel = Path(os.path.realpath(child.parent)).relative_to(root_real)
        rel_str = child.name if parent_rel == Path(".") else f"{parent_rel.as_posix()}/{child.name}"
        st = child.lstat()
        is_dir = child.is_dir()
        return {
            "name": child.name,
            "path": rel_str,
            "type": "directory" if is_dir else "file",
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "symlink": stat.S_ISLNK(st.st_mode),
            "protected": is_protected(rel_str),
        }
    except (OSError, ValueError):
        return None


@router.get("/tree")
async def tree(
    request: Request,
    path: str = Query("", description="Workspace-relative directory"),
    depth: int = Query(2, ge=1, le=6),
    show_hidden: bool = Query(False),
):
    root = _settings(request).workspace
    directory = resolve_in_workspace(root, path)
    if not directory.is_dir():
        raise HTTPException(404, f"Not a directory: {normalize_relative(path) or '/'}")
    entries, truncated = _list_dir(root, directory, depth, show_hidden)
    return {"path": normalize_relative(path), "entries": entries, "truncated": truncated}


@router.get("/stat")
async def file_stat(request: Request, path: str = Query(...)):
    root = _settings(request).workspace
    target = resolve_in_workspace(root, path)
    if not target.exists():
        raise HTTPException(404, "Not found")
    item = _entry_safe(root, target) if normalize_relative(path) else {"name": "", "path": "", "type": "directory"}
    return item


@router.get("/content")
async def read_text(request: Request, path: str = Query(...)):
    settings = _settings(request)
    ensure_not_protected(path)
    target = resolve_in_workspace(settings.workspace, path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    size = target.stat().st_size
    if size > settings.max_text_file_bytes:
        raise HTTPException(413, f"File is {size} bytes; use /files/raw for files over {settings.max_text_file_bytes}")
    data = target.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "Binary file; use /files/raw") from None
    return {"path": normalize_relative(path), "content": text, "size": size, "mtime": target.stat().st_mtime}


@router.get("/raw")
async def read_raw(request: Request, path: str = Query(...), download: bool = Query(False)):
    settings = _settings(request)
    ensure_not_protected(path)
    target = resolve_in_workspace(settings.workspace, path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    # Workspace HTML (analysis reports) is rendered by the web app in a sandboxed
    # iframe; never let it execute with the platform origin's privileges.
    if media_type in {"text/html", "image/svg+xml"}:
        headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups"
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name if download else None,
        headers=headers,
        content_disposition_type="attachment" if download else "inline",
    )


class WriteText(BaseModel):
    path: str
    content: str
    expected_mtime: float | None = Field(None, description="Reject the write if the file changed since")
    create_dirs: bool = True


@router.put("/content")
async def write_text(request: Request, body: WriteText):
    settings = _settings(request)
    ensure_not_protected(body.path)
    if not normalize_relative(body.path):
        raise HTTPException(400, "A file path is required")
    target = resolve_in_workspace(settings.workspace, body.path)
    if target.is_dir():
        raise HTTPException(409, "Path is a directory")
    if body.expected_mtime is not None and target.exists():
        if abs(target.stat().st_mtime - body.expected_mtime) > 1e-6:
            raise HTTPException(409, "File changed since it was read")
    if body.create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.sandboxd-tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(target)
    return {"path": normalize_relative(body.path), "size": target.stat().st_size, "mtime": target.stat().st_mtime}


@router.post("/upload")
async def upload(
    request: Request,
    directory: str = Form(""),
    files: list[UploadFile] = File(...),
    overwrite: bool = Form(False),
):
    settings = _settings(request)
    base = resolve_in_workspace(settings.workspace, directory)
    base.mkdir(parents=True, exist_ok=True)
    saved = []
    for upload_file in files:
        name = Path(upload_file.filename or "upload.bin").name
        rel = f"{normalize_relative(directory)}/{name}".lstrip("/")
        ensure_not_protected(rel)
        target = resolve_in_workspace(settings.workspace, rel)
        if target.exists() and not overwrite:
            raise HTTPException(409, f"{rel} already exists")
        written = 0
        tmp = target.with_name(f".{target.name}.sandboxd-tmp")
        with open(tmp, "wb") as out:
            while chunk := await upload_file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, f"{name} exceeds {settings.max_upload_bytes} bytes")
                out.write(chunk)
        tmp.replace(target)
        saved.append({"path": rel, "size": written})
    return {"saved": saved}


class PathBody(BaseModel):
    path: str


@router.post("/mkdir")
async def mkdir(request: Request, body: PathBody):
    ensure_not_protected(body.path)
    target = resolve_in_workspace(_settings(request).workspace, body.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"path": normalize_relative(body.path)}


class MoveBody(BaseModel):
    src: str
    dst: str
    overwrite: bool = False


@router.post("/move")
async def move(request: Request, body: MoveBody):
    root = _settings(request).workspace
    ensure_not_protected(body.src)
    ensure_not_protected(body.dst)
    if not normalize_relative(body.src) or not normalize_relative(body.dst):
        raise HTTPException(400, "Cannot move the workspace root")
    src = resolve_in_workspace(root, body.src)
    dst = resolve_in_workspace(root, body.dst)
    if not src.exists():
        raise HTTPException(404, "Source not found")
    if dst.exists() and not body.overwrite:
        raise HTTPException(409, "Destination exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"src": normalize_relative(body.src), "dst": normalize_relative(body.dst)}


@router.delete("")
async def delete(request: Request, path: str = Query(...), recursive: bool = Query(False)):
    root = _settings(request).workspace
    ensure_not_protected(path)
    if not normalize_relative(path):
        raise HTTPException(400, "Refusing to delete the workspace root")
    rel = normalize_relative(path)
    # Do not resolve symlinks for deletion: remove the link itself, never its target.
    lexical = Path(os.path.realpath(root)) / rel
    target = lexical if lexical.is_symlink() else resolve_in_workspace(root, rel)
    if not target.exists() and not target.is_symlink():
        raise HTTPException(404, "Not found")
    if target.is_dir() and not target.is_symlink():
        if not recursive and any(target.iterdir()):
            raise HTTPException(409, "Directory not empty; pass recursive=true")
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": rel}


def _iter_export(root: Path, base: Path):
    root_real = Path(os.path.realpath(root))
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXPORT_EXCLUDES]
        for name in filenames:
            if name in EXPORT_EXCLUDES:
                continue
            full = Path(dirpath) / name
            real = Path(os.path.realpath(full))
            if real != root_real and root_real not in real.parents:
                continue
            yield full, full.relative_to(base)


def _build_zip(root: Path, base: Path, out_path: Path) -> None:
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for full, rel in _iter_export(root, base):
            zf.write(full, rel.as_posix())


@router.get("/archive")
async def archive(request: Request, path: str = Query(""), name: str = Query("")):
    """Zip a directory (default: the whole workspace) without secrets or VCS state."""
    settings = _settings(request)
    root = settings.workspace
    base = resolve_in_workspace(root, path)
    if not base.is_dir():
        raise HTTPException(404, "Not a directory")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=settings.state_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        await asyncio.to_thread(_build_zip, root, base, tmp_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    filename = (name or base.name or "workspace") + ".zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(tmp_path.unlink, missing_ok=True),
    )
