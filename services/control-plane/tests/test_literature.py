"""Literature search: source adapters, ranking, the org library and the MCP endpoint."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from controlplane.literature import sources
from controlplane.literature.server import current_org

from .conftest import live_api_key, live_signup

OPENALEX = {
    "results": [
        {
            "display_name": "Recommender systems and echo chambers",
            "abstract_inverted_index": {"Feeds": [0], "amplify": [1], "echo": [2], "chambers": [3]},
            "doi": "https://doi.org/10.1000/ECHO.1",
            "id": "https://openalex.org/W1",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "A. Author"}}],
            "primary_location": {"source": {"display_name": "Journal of Feeds"}},
            "best_oa_location": {"pdf_url": "https://example.org/echo.pdf"},
            "cited_by_count": 12,
        }
    ]
}
ARXIV = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <published>2024-01-02T00:00:00Z</published>
    <title>Echo chambers in
      recommendation feeds</title>
    <summary>We simulate echo chambers with LLM agents.</summary>
    <author><name>B. Researcher</name></author>
    <arxiv:doi>10.1000/echo.1</arxiv:doi>
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v1"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1901.00002v1</id>
    <published>2019-01-02T00:00:00Z</published>
    <title>An old paper</title>
    <summary>Old.</summary>
  </entry>
</feed>"""
CROSSREF = {
    "message": {
        "items": [
            {
                "title": ["Polarization dynamics on social platforms"],
                "abstract": "<jats:p>Echo chambers and <i>polarization</i>.</jats:p>",
                "container-title": ["Social Science Review"],
                "DOI": "10.2000/POL.7",
                "URL": "https://doi.org/10.2000/pol.7",
                "issued": {"date-parts": [[2023, 5]]},
                "author": [{"given": "C.", "family": "Scholar"}],
                "link": [{"URL": "https://example.org/pol.pdf", "content-type": "application/pdf"}],
            }
        ]
    }
}


def mock_sources(mock: respx.MockRouter) -> None:
    mock.get("https://api.openalex.org/works").mock(return_value=httpx.Response(200, json=OPENALEX))
    mock.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(200, text=ARXIV))
    mock.get("https://api.crossref.org/works").mock(return_value=httpx.Response(200, json=CROSSREF))


async def test_source_adapters_normalise_results():
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock_sources(mock)
        [oa] = await sources.search_openalex(client, "echo chambers", 5, 2020, None, "ops@example.com")
        arxiv = await sources.search_arxiv(client, "echo chambers", 5, 2020, None)
        [cr] = await sources.search_crossref(client, "echo chambers", 5, None, 2025)
        params = mock.calls[0].request.url.params
    assert oa["abstract"] == "Feeds amplify echo chambers"  # rebuilt from the inverted index
    assert (oa["doi"], oa["year"], oa["journal"], oa["pdf_url"]) == ("10.1000/ECHO.1", 2024, "Journal of Feeds", "https://example.org/echo.pdf")
    assert params["filter"] == "from_publication_date:2020-01-01" and params["mailto"] == "ops@example.com"
    assert len(arxiv) == 1  # the 2019 entry is outside year_from
    assert arxiv[0]["title"] == "Echo chambers in recommendation feeds" and arxiv[0]["pdf_url"].endswith("2401.00001v1")
    assert cr["abstract"] == "Echo chambers and polarization ." and cr["year"] == 2023 and cr["authors"] == ["C. Scholar"]


def test_ranking_merges_duplicates_across_sources():
    a = {"title": "Recommender systems and echo chambers", "doi": "10.1000/ECHO.1", "abstract": "", "source": "openalex"}
    b = {"title": "Echo chambers in recommendation feeds", "doi": "10.1000/echo.1", "abstract": "LLM agents", "source": "arxiv", "pdf_url": "x.pdf"}
    c = {"title": "Unrelated gardening", "doi": "", "abstract": "tomatoes", "source": "crossref"}
    merged = sources.rank_and_merge("echo chambers", [[a], [b], [c]], 10)
    assert len(merged) == 2
    top = merged[0]
    assert top["source"] == "openalex" and top["pdf_url"] == "x.pdf" and top["abstract"] == "LLM agents"
    assert merged[0]["score"] > merged[1]["score"]
    assert sources.relevance("echo chambers", "Echo chambers", "") > sources.relevance("echo chambers", "", "echo chambers")


async def test_failing_sources_are_reported_not_fatal():
    async def boom():
        raise httpx.ConnectTimeout("timed out")

    async def ok():
        return [{"title": "t", "doi": "d"}]

    batches, status = await sources.gather_sources({"openalex": boom(), "local": ok()})
    assert batches == [[{"title": "t", "doi": "d"}]]
    assert status["openalex"]["ok"] is False and "ConnectTimeout" in status["openalex"]["error"]
    assert status["local"] == {"ok": True, "count": 1}


@pytest.mark.parametrize("cp", [{"literature_sources": ["openalex", "arxiv", "crossref", "local"]}], indirect=True)
async def test_service_search_combines_public_sources_and_the_org_library(cp):
    alice = await cp.signup("alice@example.com")
    bob = await cp.signup("bob@example.com")
    service = cp.ctx.literature
    token = current_org.set(alice.org_id)
    try:
        assert (await service.ingest("Internal memo: echo chambers grow when feeds optimise engagement.", "Lab memo on echo chambers", "", "", 2025, ["Lab"]))[
            "success"
        ]
        with respx.mock() as mock:
            mock_sources(mock)
            result = await service.search("echo chambers", 10, None, None, None)
    finally:
        current_org.reset(token)
    titles = [r["title"] for r in result["results"]]
    assert "Lab memo on echo chambers" in titles and "Polarization dynamics on social platforms" in titles
    assert all(s["ok"] for s in result["sources"].values())
    # Another organisation never sees Alice's library.
    token = current_org.set(bob.org_id)
    try:
        result = await service.search("echo chambers", 10, None, None, ["local"])
    finally:
        current_org.reset(token)
    assert result["results"] == []


def test_mcp_endpoint_with_the_upstream_client(live_cp, monkeypatch):
    monkeypatch.setenv("AGENTSOCIETY_LLM_API_KEY", "unused")
    monkeypatch.setenv("AGENTSOCIETY_LLM_API_BASE", "http://unused.invalid/v1")
    from agentsociety2.skills.literature import mcp_client

    alice = live_signup(live_cp, "alice@example.com")
    key = live_api_key(live_cp, alice)
    url = f"{live_cp.base_url}/literature/mcp/"  # agentsociety2 always uses the trailing slash

    async def scenario():
        tools = await mcp_client.list_literature_mcp_tools(mcp_url=url, api_key=key)
        status = await mcp_client.call_literature_status_mcp(mcp_url=url, api_key=key)
        ingested = await mcp_client.call_literature_mcp_tool(
            mcp_url=url,
            api_key=key,
            kind="ingest",
            arguments={"text": "Agent-based models of opinion dynamics show echo chambers.", "title": "Opinion dynamics notes", "year": 2026},
        )
        found = await mcp_client.call_literature_search_mcp(mcp_url=url, api_key=key, arguments={"query": "opinion dynamics echo chambers", "limit": 5})
        return tools, status, ingested, found

    tools, status, ingested, found = asyncio.run(scenario())
    assert sorted(tools) == ["literature_ingest_text", "literature_search", "literature_status"]
    assert status["status"] == "ok" and status["sources"]["local"]["enabled"] is True
    assert ingested["success"] is True
    assert found["total"] == 1 and found["results"][0]["title"] == "Opinion dynamics notes"
    assert found["sources"] == {"local": {"ok": True, "count": 1}}

    # Unauthenticated requests are refused before reaching the MCP server.
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401 and "Missing API key" in json.dumps(r.json())
    # The bare path works too (no redirect).
    r = httpx.post(
        url.rstrip("/"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"authorization": f"Bearer {key}", "accept": "application/json, text/event-stream"},
    )
    assert r.status_code != 307


async def test_rest_search_and_library(cp):
    alice = await cp.signup("alice@example.com")
    viewer = await cp.signup("viewer@example.com")
    stranger = await cp.signup("stranger@example.com")
    await cp.add_member(alice, viewer, "viewer")
    base = f"/api/v1/orgs/{alice.org_id}/literature"
    r = await cp.client.post(
        f"{base}/library", json={"text": "Echo chambers emerge in simulated feeds.", "title": "Feed simulation notes", "year": 2026}, headers=alice.h
    )
    assert r.status_code == 200
    doc_id = r.json()["id"]
    assert (await cp.client.post(f"{base}/library", json={"text": "x"}, headers=viewer.h)).status_code == 403
    found = (await cp.client.post(f"{base}/search", json={"query": "echo chambers"}, headers=viewer.h)).json()
    assert [r["title"] for r in found["results"]] == ["Feed simulation notes"]
    assert (await cp.client.post(f"{base}/search", json={"query": "x", "sources": ["google"]}, headers=alice.h)).status_code == 400
    assert (await cp.client.get(f"{base}/library", headers=stranger.h)).status_code == 404
    listed = (await cp.client.get(f"{base}/library", headers=viewer.h)).json()["documents"]
    assert listed[0]["characters"] == len("Echo chambers emerge in simulated feeds.") and "text" not in listed[0]
    assert (await cp.client.get(f"{base}/library/{doc_id}", headers=alice.h)).json()["text"].startswith("Echo")
    assert (await cp.client.delete(f"{base}/library/{doc_id}", headers=alice.h)).status_code == 200
    assert (await cp.client.get(f"{base}/library", headers=alice.h)).json()["documents"] == []
