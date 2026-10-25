"""Dataset hub, wire-compatible with the upstream ``use-dataset`` / ``create-dataset`` skills.

Public read API (``/api/v1/data/...``) and authenticated author API
(``/api/user/data/...``) mirror what the skills call on agentsociety2.fiblab.net,
so the unmodified clients work against this platform (the skill patches only
change the server URL and pass the project token).

Visibility: datasets are private to their organisation once uploaded; submitting
one for review and approval by a platform admin publishes it to everyone.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..deps import get_db, require_platform_admin, resolve_principal
from ..gateway.auth import GatewayError
from ..models import Dataset, DatasetUpload, Membership
from ..security import PROJECT_TOKEN_PREFIX, parse_composite
from ..storage import LocalBlobStore

router = APIRouter(tags=["datasets"])

CATEGORIES = [
    ("agent_profiles", "Agent profiles"),
    ("surveys", "Surveys"),
    ("experiments", "Experiments"),
    ("literature", "Literature"),
    ("simulation_results", "Simulation results"),
    ("other", "Other"),
]
PART_SIZE = 10 * 1024 * 1024  # must match create.py
MAX_PACKAGE_BYTES = 5 * 1024 * 1024 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,99}$")


class Identity(BaseModel):
    org_ids: list[str] = Field(default_factory=list)
    acting_org_id: str | None = None
    user_id: str | None = None


async def identity(request: Request, db: AsyncSession) -> Identity:
    """Who is asking: a user (JWT), an API key, or a sandbox (project token). May be anonymous."""
    token = request.headers.get("authorization", "").partition(" ")[2].strip()
    composite = parse_composite(token) if token else None
    if composite and composite.kind == PROJECT_TOKEN_PREFIX:
        try:
            caller = await request.app.state.ctx.gateway.callers.resolve(request)
        except GatewayError:
            raise HTTPException(401, "Invalid project token") from None
        return Identity(org_ids=[caller.org_id], acting_org_id=caller.org_id)
    principal = await resolve_principal(request, db) if token else None
    if principal is None:
        if token:
            raise HTTPException(401, "Invalid credentials")
        return Identity()
    if principal.api_key is not None:
        return Identity(org_ids=[principal.api_key.org_id], acting_org_id=principal.api_key.org_id, user_id=principal.api_key.user_id)
    rows = (await db.execute(select(Membership).where(Membership.user_id == principal.user.id))).scalars().all()
    org_ids = [m.org_id for m in rows if m.role != "viewer"] + [m.org_id for m in rows if m.role == "viewer"]
    requested = request.headers.get("x-org-id") or request.query_params.get("org_id")
    acting = requested if requested in {m.org_id for m in rows if m.role != "viewer"} else next(
        (m.org_id for m in rows if m.role != "viewer"), None
    )
    return Identity(org_ids=org_ids, acting_org_id=acting, user_id=principal.user.id)


def _public(ds: Dataset) -> dict[str, Any]:
    return {
        "id": ds.id,
        "name": ds.name,
        "description": ds.description,
        "category": ds.category,
        "version": ds.version,
        "tags": ds.tags or [],
        "author": ds.author,
        "license": ds.license,
        "visibility": ds.visibility,
        "status": ds.status,
        "package_size_bytes": ds.package_size_bytes,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
        "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
        "published_at": ds.published_at.isoformat() if ds.published_at else None,
    }


def _readable_filter(who: Identity):
    public = (Dataset.status == "published") & (Dataset.visibility == "public")
    if who.org_ids:
        return or_(public, Dataset.org_id.in_(who.org_ids))
    return public


async def _get_readable(db: AsyncSession, who: Identity, dataset_id: str) -> Dataset:
    ds = await db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "Dataset not found")
    if not ((ds.status == "published" and ds.visibility == "public") or ds.org_id in who.org_ids):
        raise HTTPException(404, "Dataset not found")
    return ds


# ------------------------------------------------------------------ read API


@router.get("/api/v1/data/categories")
async def categories():
    return {"categories": [{"value": v, "label": label} for v, label in CATEGORIES]}


@router.get("/api/v1/data/datasets")
async def list_datasets(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    skip: int = Query(0, ge=0),
    category: str | None = None,
    tags: str | None = None,
    q: str | None = Query(None, max_length=200),
):
    who = await identity(request, db)
    stmt = select(Dataset).where(_readable_filter(who), Dataset.status != "draft")
    if category:
        stmt = stmt.where(Dataset.category == category)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(Dataset.name.ilike(like), Dataset.description.ilike(like), Dataset.id.ilike(like)))
    rows = (await db.execute(stmt.order_by(Dataset.updated_at.desc()))).scalars().all()
    if tags:
        wanted = {t.strip() for t in tags.split(",") if t.strip()}
        rows = [r for r in rows if wanted & set(r.tags or [])]
    page = rows[skip : skip + limit]
    return {"datasets": [_public(r) for r in page], "total": len(rows), "limit": limit, "skip": skip}


@router.get("/api/v1/data/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    return _public(await _get_readable(db, await identity(request, db), dataset_id))


@router.get("/api/v1/data/datasets/{dataset_id}/readme")
async def get_readme(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    ds = await _get_readable(db, await identity(request, db), dataset_id)
    return {"content": ds.readme or ""}


@router.get("/api/v1/data/datasets/{dataset_id}/file-tree")
async def get_file_tree(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    ds = await _get_readable(db, await identity(request, db), dataset_id)
    return {"files": (ds.file_tree or {}).get("files", [])}


@router.post("/api/v1/data/datasets/{dataset_id}/download")
async def download(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    ds = await _get_readable(db, await identity(request, db), dataset_id)
    if not ds.package_key:
        raise HTTPException(409, "Dataset has no uploaded package")
    url = await request.app.state.ctx.blobs.download_url(ds.package_key, f"{ds.id}.zip")
    return {"url": url, "expires_in": 3600}


# ----------------------------------------------------------------- author API


class CreateDataset(BaseModel):
    id: str
    name: str = Field(..., max_length=300)
    description: str = ""
    category: str = "other"
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    author: str = ""
    license: str = "CC BY 4.0"


async def _owned(db: AsyncSession, who: Identity, dataset_id: str) -> Dataset:
    ds = await db.get(Dataset, dataset_id)
    if ds is None or ds.org_id != who.acting_org_id:
        raise HTTPException(404, "Dataset not found")
    return ds


def _require_author(who: Identity) -> None:
    if not who.acting_org_id:
        raise HTTPException(401, "Authentication with an organization is required")


@router.post("/api/user/data/datasets")
async def create_dataset(body: CreateDataset, request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    if not SLUG_RE.fullmatch(body.id):
        raise HTTPException(400, "id must be a lowercase slug (a-z, 0-9, '-', '_')")
    if body.category not in {c for c, _ in CATEGORIES}:
        raise HTTPException(400, f"category must be one of {[c for c, _ in CATEGORIES]}")
    existing = await db.get(Dataset, body.id)
    if existing is not None:
        if existing.org_id != who.acting_org_id:
            raise HTTPException(409, "A dataset with this id already exists")
        for field in ("name", "description", "category", "version", "tags", "author", "license"):
            setattr(existing, field, getattr(body, field))
        await db.commit()
        return _public(existing)
    ds = Dataset(org_id=who.acting_org_id, owner_user_id=who.user_id, **body.model_dump())
    db.add(ds)
    await db.commit()
    return _public(ds)


@router.get("/api/user/data/datasets")
async def my_datasets(request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    rows = (await db.execute(select(Dataset).where(Dataset.org_id.in_(who.org_ids)).order_by(Dataset.updated_at.desc()))).scalars().all()
    return {"datasets": [_public(r) for r in rows]}


class InitUpload(BaseModel):
    file_size: int = Field(..., gt=0)


@router.post("/api/user/data/datasets/{dataset_id}/init-upload")
async def init_upload(dataset_id: str, body: InitUpload, request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    ds = await _owned(db, who, dataset_id)
    if body.file_size > MAX_PACKAGE_BYTES:
        raise HTTPException(413, "Package too large")
    parts = max(1, math.ceil(body.file_size / PART_SIZE))
    upload = DatasetUpload(dataset_id=ds.id, part_size=PART_SIZE, total_size=body.file_size, parts=parts)
    db.add(upload)
    await db.commit()
    blobs = request.app.state.ctx.blobs
    key = f"datasets/{ds.id}/{upload.id}.zip"
    part_urls = [{"part_number": n, "upload_url": await blobs.part_upload_url(key, n)} for n in range(1, parts + 1)]
    return {"upload_id": upload.id, "part_size": PART_SIZE, "part_urls": part_urls}


@router.put("/api/v1/blobs/upload/{key:path}")
async def put_part(key: str, request: Request, part: int = Query(..., ge=1), exp: int = Query(...), sig: str = Query(...)):
    """Signed part upload for the local blob backend (S3 uses presigned URLs instead)."""
    blobs = request.app.state.ctx.blobs
    if not isinstance(blobs, LocalBlobStore) or not blobs.verify_part(key, part, exp, sig):
        raise HTTPException(403, "Invalid or expired upload URL")
    data = await request.body()
    if len(data) > PART_SIZE:
        raise HTTPException(413, "Part too large")
    blobs.write_part(key, part, data)
    return {"part_number": part, "size": len(data)}


@router.get("/api/v1/blobs/download/{key:path}")
async def get_blob(key: str, request: Request, exp: int = Query(...), sig: str = Query(...), filename: str = Query("download.zip")):
    blobs = request.app.state.ctx.blobs
    if not isinstance(blobs, LocalBlobStore) or not blobs.verify_download(key, exp, sig):
        raise HTTPException(403, "Invalid or expired download URL")
    path = blobs.path(key)
    if not path.exists():
        raise HTTPException(404, "Not found")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:100] or "download.zip"
    return FileResponse(path, media_type="application/zip", filename=safe_name)


class CompleteUpload(BaseModel):
    upload_id: str
    parts: list[dict] = Field(default_factory=list)


def inspect_package(path: Path, dataset_id: str) -> dict[str, Any]:
    """Validate a dataset ZIP and extract README + file tree (as the upstream hub does)."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Upload is not a valid ZIP archive") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise HTTPException(400, "Archive contains unsafe paths")
        meta_name = next((n for n in names if Path(n).name == "dataset.json"), None)
        if meta_name is None:
            raise HTTPException(400, "dataset.json not found in the archive")
        try:
            meta = json.loads(zf.read(meta_name))
        except json.JSONDecodeError:
            raise HTTPException(400, "dataset.json is not valid JSON") from None
        if meta.get("id") and meta["id"] != dataset_id:
            raise HTTPException(400, f"dataset.json id {meta['id']!r} does not match {dataset_id!r}")
        readme_name = next((n for n in names if Path(n).name.lower() == "readme.md"), None)
        readme = zf.read(readme_name).decode("utf-8", errors="replace")[:200_000] if readme_name else ""
        prefix = ""
        if names and all("/" in n for n in names):
            first = names[0].split("/", 1)[0] + "/"
            if all(n.startswith(first) for n in names):
                prefix = first
        files = [{"path": i.filename[len(prefix):], "size_bytes": i.file_size} for i in infos]
    return {"meta": meta, "readme": readme, "files": files}


@router.post("/api/user/data/datasets/{dataset_id}/complete-upload")
async def complete_upload(dataset_id: str, body: CompleteUpload, request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    ds = await _owned(db, who, dataset_id)
    upload = await db.get(DatasetUpload, body.upload_id)
    if upload is None or upload.dataset_id != ds.id or upload.status != "open":
        raise HTTPException(404, "Upload not found")
    blobs = request.app.state.ctx.blobs
    key = f"datasets/{ds.id}/{upload.id}.zip"
    try:
        size = await blobs.complete_parts(key, upload.parts)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from None
    info = inspect_package(await blobs.open_local(key), ds.id)
    previous = ds.package_key
    ds.package_key = key
    ds.package_size_bytes = size
    ds.readme = info["readme"]
    ds.file_tree = {"files": info["files"]}
    meta = info["meta"]
    for field in ("name", "description", "category", "version", "author", "license"):
        if meta.get(field):
            setattr(ds, field, meta[field])
    if isinstance(meta.get("tags"), list):
        ds.tags = [str(t) for t in meta["tags"]]
    if ds.status in ("draft", "rejected"):
        ds.status = "uploaded"
    upload.status = "completed"
    await db.commit()
    if previous and previous != key:
        await blobs.delete(previous)
    return {**_public(ds), "files": len(info["files"])}


@router.post("/api/user/data/datasets/{dataset_id}/submit")
async def submit(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    ds = await _owned(db, who, dataset_id)
    if not ds.package_key:
        raise HTTPException(400, "Please upload a file first")
    ds.status = "pending_review"
    ds.visibility = "public"
    await db.commit()
    return _public(ds)


@router.delete("/api/user/data/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    who = await identity(request, db)
    _require_author(who)
    ds = await _owned(db, who, dataset_id)
    if ds.package_key:
        await request.app.state.ctx.blobs.delete(ds.package_key)
    await db.delete(ds)
    await db.commit()
    return {"deleted": dataset_id}


class Review(BaseModel):
    approve: bool
    note: str = ""


@router.post("/api/v1/admin/datasets/{dataset_id}/review", dependencies=[Depends(require_platform_admin)])
async def review(dataset_id: str, body: Review, db: AsyncSession = Depends(get_db)):
    ds = await db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "Dataset not found")
    if body.approve:
        ds.status = "published"
        ds.published_at = utcnow()
    else:
        ds.status = "rejected"
        ds.visibility = "org"
    ds.review_note = body.note
    await db.commit()
    return _public(ds)
