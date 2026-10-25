"""Literature search and the organization library, for the web app.

Sandboxes use the same service over MCP (``/literature/mcp``); this REST surface
lets people search and curate the library from the browser.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal, ctx, get_db, get_principal, org_access
from ..literature.server import ALL_SOURCES, current_org
from ..models import LiteratureDoc

router = APIRouter(prefix="/api/v1/orgs/{org_id}/literature", tags=["literature"])


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    limit: int = Field(10, ge=1, le=50)
    year_from: int | None = Field(None, ge=1500, le=2100)
    year_to: int | None = Field(None, ge=1500, le=2100)
    sources: list[str] | None = None


@router.post("/search")
async def search(org_id: str, body: SearchBody, request: Request, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    if body.sources and not set(body.sources) <= set(ALL_SOURCES):
        raise HTTPException(400, f"sources must be among {list(ALL_SOURCES)}")
    token = current_org.set(org_id)
    try:
        return await ctx(request).literature.search(body.query, body.limit, body.year_from, body.year_to, body.sources)
    finally:
        current_org.reset(token)


def doc_out(doc: LiteratureDoc, *, text: bool = False) -> dict:
    out = {
        "id": doc.id,
        "title": doc.title,
        "doi": doc.doi,
        "url": doc.url,
        "year": doc.year,
        "authors": doc.authors or [],
        "characters": len(doc.text or ""),
        "excerpt": (doc.text or "")[:400],
        "created_at": doc.created_at,
    }
    if text:
        out["text"] = doc.text
    return out


@router.get("/library")
async def library(org_id: str, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    docs = (await db.execute(select(LiteratureDoc).where(LiteratureDoc.org_id == org_id).order_by(LiteratureDoc.created_at.desc()))).scalars().all()
    return {"documents": [doc_out(d) for d in docs]}


class IngestBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=5_000_000)
    title: str = Field("", max_length=1000)
    doi: str = Field("", max_length=300)
    url: str = Field("", max_length=1000)
    year: int | None = Field(None, ge=1500, le=2100)
    authors: list[str] = Field(default_factory=list)


@router.post("/library")
async def ingest(org_id: str, body: IngestBody, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "member")
    doc = LiteratureDoc(org_id=org_id, title=body.title or body.text[:120], text=body.text, doi=body.doi, url=body.url, year=body.year, authors=body.authors)
    db.add(doc)
    await db.commit()
    return doc_out(doc)


@router.get("/library/{doc_id}")
async def get_doc(org_id: str, doc_id: int, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id)
    doc = await db.get(LiteratureDoc, doc_id)
    if doc is None or doc.org_id != org_id:
        raise HTTPException(404, "Document not found")
    return doc_out(doc, text=True)


@router.delete("/library/{doc_id}")
async def delete_doc(org_id: str, doc_id: int, principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    await org_access(db, principal, org_id, "member")
    doc = await db.get(LiteratureDoc, doc_id)
    if doc is None or doc.org_id != org_id:
        raise HTTPException(404, "Document not found")
    await db.delete(doc)
    await db.commit()
    return {"deleted": doc_id}
