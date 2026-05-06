"""denvergov_search.py — Tavily-backed search restricted to denvergov.org.

Used by the `search_denver_gov` agent tool when a user query asks for
bureaucratic info (forms, fees, office hours, contact pages, programs,
services) that lives on the city's official website but isn't in our
internal Qdrant catalog.

Pattern mirrors weather.py: env-var-configurable in-memory TTL cache keyed
on the normalized query, error-as-data return contract so the tool_agent
loop never sees an exception (it expects dicts).

Uses the Tavily SDK directly (`AsyncTavilyClient`) rather than the
LangChain `TavilySearchResults` wrapper — simpler, one fewer indirection
layer, and avoids pulling in `langchain-community` solely for one tool.

Returns a `DenverGovSearchResult` Pydantic model. On failure the model has
`error` populated and `results` empty — callers should branch on `error`
rather than catching exceptions.
"""

from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
from pydantic import BaseModel
from tavily import AsyncTavilyClient

load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 3
INCLUDE_DOMAINS = ["denvergov.org"]
SNIPPET_MAX_CHARS = 300

# Discriminator stamped on every `sources` SSE entry produced by the
# search_denver_gov tool. Lets the frontend render Tavily-derived
# denvergov.org links differently from retrieval-driven catalog sources
# (which carry doc_types like denver_park, neighborhood_demographics, etc.).
DOC_TYPE = "denvergov_search_result"

# denvergov.org pages are mostly static (forms, services, office hours), so a
# 24-hour cache is plenty. Tavily charges per query; the cache directly
# controls API spend.
TAVILY_CACHE_TTL_SECONDS = int(os.getenv("TAVILY_CACHE_TTL_SECONDS", "86400"))

# Keyed on (normalized_query, max_results) so the same question with
# different request sizes doesn't cross-contaminate.
_search_cache: dict[tuple[str, int], tuple[float, dict]] = {}


class DenverGovHit(BaseModel):
    title: str
    url: str
    snippet: str
    score: float | None = None


class DenverGovSearchResult(BaseModel):
    query: str
    results: list[DenverGovHit]
    error: str | None = None


def _build_tavily_client() -> AsyncTavilyClient:
    """Factory split out so tests can patch it without touching the rest of
    the module. AsyncTavilyClient reads TAVILY_API_KEY from environment when
    api_key is not passed explicitly."""
    return AsyncTavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


async def search_denvergov_for_query(
    query: str, max_results: int = DEFAULT_MAX_RESULTS
) -> DenverGovSearchResult:
    """Run a Tavily web search restricted to denvergov.org. Cached for
    TAVILY_CACHE_TTL_SECONDS by normalized (query, max_results)."""
    cache_key = (query.lower().strip(), max_results)
    now = time.time()
    cached = _search_cache.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if now - cached_at < TAVILY_CACHE_TTL_SECONDS:
            return DenverGovSearchResult.model_validate(payload)

    try:
        client = _build_tavily_client()
        raw = await client.search(
            query=query,
            max_results=max_results,
            include_domains=INCLUDE_DOMAINS,
        )
    except Exception as exc:
        logger.warning("Tavily search failed for %r: %s", query, exc)
        return DenverGovSearchResult(query=query, results=[], error=str(exc))

    hits: list[DenverGovHit] = []
    for item in (raw or {}).get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        hits.append(
            DenverGovHit(
                title=(item.get("title") or "").strip(),
                url=url,
                snippet=(item.get("content") or "")[:SNIPPET_MAX_CHARS],
                score=item.get("score"),
            )
        )
    result = DenverGovSearchResult(query=query, results=hits)
    _search_cache[cache_key] = (now, result.model_dump())
    return result


def clear_caches() -> None:
    """Clear the in-process search cache. Useful for tests."""
    _search_cache.clear()
