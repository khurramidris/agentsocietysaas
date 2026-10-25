"""Literature search MCP server (Streamable HTTP), compatible with agentsociety2.

agentsociety2's literature client connects to ``LITERATURE_SEARCH_MCP_URL`` (a URL
containing ``/mcp/``), lists tools and calls ``literature_search`` /
``literature_status`` / ``literature_ingest_text``, parsing the JSON text of the
first content block. This server implements those tools on public scholarly APIs
plus an organisation-private index, authenticated with the same project tokens
and API keys as the LLM gateway.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from ..gateway.auth import GatewayError
from ..models import LiteratureDoc
from .sources import gather_sources, rank_and_merge, relevance, search_arxiv, search_crossref, search_openalex

if TYPE_CHECKING:
    from ..context import AppContext

current_org: ContextVar[str | None] = ContextVar("literature_org", default=None)

ALL_SOURCES = ("openalex", "arxiv", "crossref", "local")


class LiteratureService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.http = httpx.AsyncClient(
            timeout=ctx.settings.literature_timeout_s,
            headers={"User-Agent": f"{ctx.settings.brand_name}-literature/0.1 (mailto:{ctx.settings.openalex_mailto or 'unset'})"},
            follow_redirects=True,
        )
        self.server = self._build_server()
        self.asgi = self.server.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def close(self) -> None:
        await self.http.aclose()

    # ----------------------------------------------------------------- tools

    async def search(self, query: str, limit: int, year_from: int | None, year_to: int | None, sources: list[str] | None) -> dict[str, Any]:
        org_id = current_org.get()
        enabled = [s for s in (sources or self.ctx.settings.literature_sources) if s in ALL_SOURCES]
        mailto = self.ctx.settings.openalex_mailto
        tasks: dict[str, Any] = {}
        if "openalex" in enabled:
            tasks["openalex"] = search_openalex(self.http, query, limit, year_from, year_to, mailto)
        if "arxiv" in enabled:
            tasks["arxiv"] = search_arxiv(self.http, query, limit, year_from, year_to)
        if "crossref" in enabled:
            tasks["crossref"] = search_crossref(self.http, query, limit, year_from, year_to, mailto)
        if "local" in enabled and org_id:
            tasks["local"] = self.search_local(org_id, query, limit, year_from, year_to)
        batches, status = await gather_sources(tasks)
        results = rank_and_merge(query, batches, limit)
        return {"results": results, "total": len(results), "query": query, "sources": status}

    async def search_local(self, org_id: str, query: str, limit: int, year_from: int | None, year_to: int | None) -> list[dict[str, Any]]:
        async with self.ctx.db.sessionmaker() as db:
            docs = (await db.execute(select(LiteratureDoc).where(LiteratureDoc.org_id == org_id))).scalars().all()
        scored = []
        for doc in docs:
            if (year_from and doc.year and doc.year < year_from) or (year_to and doc.year and doc.year > year_to):
                continue
            score = relevance(query, doc.title, doc.text[:20000])
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "title": doc.title,
                        "abstract": doc.text[:1500],
                        "journal": "",
                        "doi": doc.doi,
                        "url": doc.url,
                        "year": doc.year,
                        "authors": doc.authors or [],
                        "source": "local",
                        "source_name": "Organization library",
                        "chunks": [{"content": doc.text[:2000], "similarity_score": score, "document_id": str(doc.id)}],
                    },
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    async def ingest(self, text: str, title: str, doi: str, url: str, year: int | None, authors: list[str]) -> dict[str, Any]:
        org_id = current_org.get()
        if not org_id:
            return {"success": False, "error": "no organization context"}
        async with self.ctx.db.sessionmaker() as db:
            doc = LiteratureDoc(org_id=org_id, title=title or text[:120], text=text, doi=doi, url=url, year=year, authors=authors)
            db.add(doc)
            await db.commit()
            return {"success": True, "document_id": str(doc.id), "characters": len(text)}

    def _build_server(self) -> MCPServer:
        server = MCPServer(
            name="literature",
            instructions="Academic literature search over OpenAlex, arXiv, Crossref and the organization's own library.",
        )
        service = self

        @server.tool(
            name="literature_search",
            description="Search academic literature. Returns JSON {results: [...], total, query, sources}.",
            structured_output=False,
        )
        async def literature_search(
            query: str,
            limit: int = 10,
            year_from: int | None = None,
            year_to: int | None = None,
            sources: list[str] | None = None,
            similarity_threshold: float | None = None,
            vector_similarity_weight: float | None = None,
            chunk_content_limit: int | None = None,
            relevant_content_limit: int | None = None,
            max_chunks_per_article: int | None = None,
        ) -> str:
            limit = max(1, min(int(limit or 10), 50))
            result = await service.search(query, limit, year_from, year_to, sources)
            if similarity_threshold is not None:
                result["results"] = [r for r in result["results"] if r.get("score", 0) >= similarity_threshold]
                result["total"] = len(result["results"])
            if max_chunks_per_article == 0:
                for r in result["results"]:
                    r.pop("chunks", None)
            return json.dumps(result, ensure_ascii=False)

        @server.tool(name="literature_status", description="Health and data sources of the literature service.", structured_output=False)
        async def literature_status() -> str:
            return json.dumps(
                {
                    "status": "ok",
                    "time": int(time.time()),
                    "sources": {name: {"enabled": name in service.ctx.settings.literature_sources} for name in ALL_SOURCES},
                }
            )

        @server.tool(
            name="literature_ingest_text",
            description="Add a text (e.g. a paper's full text) to the organization's private literature library.",
            structured_output=False,
        )
        async def literature_ingest_text(
            text: str, title: str = "", doi: str = "", url: str = "", year: int | None = None, authors: list[str] | None = None
        ) -> str:
            return json.dumps(await service.ingest(text, title, doi, url, year, authors or []))

        return server


class AuthenticatedMCP:
    """ASGI wrapper: authenticate like the gateway, bind the org for tool calls."""

    def __init__(self, ctx: AppContext, service: LiteratureService) -> None:
        self.ctx = ctx
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.service.asgi(scope, receive, send)
            return
        from fastapi import Request

        request = Request(scope, receive)
        try:
            caller = await self.ctx.gateway.callers.resolve(request)
        except GatewayError as err:
            body = json.dumps({"error": err.message}).encode()
            await send({"type": "http.response.start", "status": err.status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        token = current_org.set(caller.org_id)
        try:
            await self.service.asgi(scope, receive, send)
        finally:
            current_org.reset(token)
