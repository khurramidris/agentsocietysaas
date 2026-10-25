"""Academic literature sources behind the literature MCP server.

Public, keyless scholarly APIs replace the upstream project's hosted search
service: OpenAlex (``api.openalex.org``), arXiv (``export.arxiv.org``) and Crossref
(``api.crossref.org``), plus an organisation-private ``local`` index of texts
ingested through ``literature_ingest_text``. Results are normalised to the shape
agentsociety2's literature client expects (``title``, ``abstract``, ``doi``,
``url``, ``year``, ``authors``, ``score``, ``source``, ``pdf_url``...).
"""

from __future__ import annotations

import asyncio
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

import httpx

_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has in is it its of on or that the to was were will with how what why when does do".split()
)


def terms(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def relevance(query: str, title: str, abstract: str) -> float:
    """Cheap lexical relevance in [0, 1]: title hits weigh more than abstract hits."""
    q = set(terms(query))
    if not q:
        return 0.0
    t = Counter(terms(title))
    a = Counter(terms(abstract))
    score = sum(2.0 * min(t[w], 1) + 1.0 * min(a[w], 1) for w in q)
    return round(min(score / (3.0 * len(q)), 1.0), 4)


def _clean_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I)


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


async def search_openalex(client: httpx.AsyncClient, query: str, limit: int, year_from: int | None, year_to: int | None, mailto: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"search": query, "per_page": min(limit, 50)}
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    if mailto:
        params["mailto"] = mailto
    resp = await client.get("https://api.openalex.org/works", params=params)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("results", []):
        loc = item.get("primary_location") or {}
        oa = item.get("open_access") or {}
        best = item.get("best_oa_location") or {}
        source = (loc.get("source") or {}).get("display_name", "")
        out.append(
            {
                "title": item.get("display_name") or item.get("title") or "",
                "abstract": _openalex_abstract(item.get("abstract_inverted_index")),
                "journal": source,
                "doi": _clean_doi(item.get("doi")),
                "url": item.get("doi") or item.get("id") or "",
                "year": item.get("publication_year"),
                "authors": [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])][:20],
                "source": "openalex",
                "source_name": "OpenAlex",
                "cited_by_count": item.get("cited_by_count"),
                "open_access": oa,
                "pdf_url": best.get("pdf_url") or loc.get("pdf_url"),
                "best_oa_location": best or None,
            }
        )
    return out


_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


async def search_arxiv(client: httpx.AsyncClient, query: str, limit: int, year_from: int | None, year_to: int | None) -> list[dict[str, Any]]:
    words = terms(query)[:8] or [query]
    search = " AND ".join(f"all:{w}" for w in words)
    resp = await client.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": search, "start": 0, "max_results": min(limit, 50), "sortBy": "relevance"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out = []
    for entry in root.findall("a:entry", _ATOM):
        published = entry.findtext("a:published", default="", namespaces=_ATOM)
        year = int(published[:4]) if published[:4].isdigit() else None
        if (year_from and year and year < year_from) or (year_to and year and year > year_to):
            continue
        pdf = next((link.get("href") for link in entry.findall("a:link", _ATOM) if link.get("title") == "pdf"), None)
        abs_url = entry.findtext("a:id", default="", namespaces=_ATOM)
        out.append(
            {
                "title": " ".join((entry.findtext("a:title", default="", namespaces=_ATOM) or "").split()),
                "abstract": " ".join((entry.findtext("a:summary", default="", namespaces=_ATOM) or "").split()),
                "journal": "arXiv",
                "doi": _clean_doi(entry.findtext("arxiv:doi", default="", namespaces=_ATOM)),
                "url": abs_url,
                "year": year,
                "authors": [a.findtext("a:name", default="", namespaces=_ATOM) for a in entry.findall("a:author", _ATOM)][:20],
                "source": "arxiv",
                "source_name": "arXiv",
                "pdf_url": pdf,
                "open_access": {"is_oa": True},
            }
        )
    return out


async def search_crossref(client: httpx.AsyncClient, query: str, limit: int, year_from: int | None, year_to: int | None, mailto: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query": query, "rows": min(limit, 50)}
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}")
    if year_to:
        filters.append(f"until-pub-date:{year_to}")
    if filters:
        params["filter"] = ",".join(filters)
    if mailto:
        params["mailto"] = mailto
    resp = await client.get("https://api.crossref.org/works", params=params)
    resp.raise_for_status()
    out = []
    for item in (resp.json().get("message") or {}).get("items", []):
        issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        abstract = re.sub(r"<[^>]+>", " ", item.get("abstract") or "")
        out.append(
            {
                "title": " ".join((item.get("title") or [""])[0].split()),
                "abstract": " ".join(abstract.split()),
                "journal": (item.get("container-title") or [""])[0],
                "doi": _clean_doi(item.get("DOI")),
                "url": item.get("URL", ""),
                "year": issued[0] if issued else None,
                "authors": [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in item.get("author", [])][:20],
                "source": "crossref",
                "source_name": "Crossref",
                "pdf_url": next((link.get("URL") for link in item.get("link", []) if link.get("content-type") == "application/pdf"), None),
            }
        )
    return out


def rank_and_merge(query: str, batches: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Dedupe by DOI (else normalised title), score lexically, keep the best ``limit``."""
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for rank, item in enumerate(batch):
            key = item.get("doi", "").lower() or re.sub(r"\W+", " ", item.get("title", "").lower()).strip()
            if not key:
                continue
            score = 0.7 * relevance(query, item.get("title", ""), item.get("abstract", "")) + 0.3 / math.sqrt(rank + 1)
            item = {**item, "score": round(score, 4)}
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            else:
                for field, value in item.items():
                    if not existing.get(field) and value:
                        existing[field] = value
                existing["score"] = max(existing["score"], item["score"])
    return sorted(merged.values(), key=lambda r: r["score"], reverse=True)[:limit]


async def gather_sources(tasks: dict[str, Any]) -> tuple[list[list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    names = list(tasks)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    batches = []
    status: dict[str, dict[str, Any]] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, Exception):
            status[name] = {"ok": False, "error": f"{type(result).__name__}: {result}"[:300]}
        else:
            batches.append(result)
            status[name] = {"ok": True, "count": len(result)}
    return batches, status
