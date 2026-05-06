"""Unit tests for app/tools/denvergov_search.

Mocks `_build_tavily_client` so no real Tavily API calls are made. The
test seam is the factory function in the module — patching it gives us
full control over what `client.search` returns."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools import denvergov_search
from app.tools.denvergov_search import (
    DenverGovSearchResult,
    search_denvergov_for_query,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean cache so cache-hit tests are hermetic."""
    denvergov_search.clear_caches()
    yield
    denvergov_search.clear_caches()


def _mock_client(return_value=None, side_effect=None) -> MagicMock:
    """Build a MagicMock that mimics AsyncTavilyClient: search() is async."""
    client = MagicMock()
    client.search = AsyncMock(return_value=return_value, side_effect=side_effect)
    return client


def _tavily_response(results: list[dict], query: str = "q") -> dict:
    """Shape of AsyncTavilyClient.search() — a dict with `query`, `results`, etc."""
    return {"query": query, "results": results}


@pytest.mark.asyncio
async def test_search_returns_hits_for_valid_query():
    raw = _tavily_response([
        {
            "title": "Pay a parking ticket",
            "url": "https://www.denvergov.org/Government/Agencies-Departments-Offices/Pay-a-ticket",
            "content": "Pay your Denver parking ticket online via the city's portal...",
            "score": 0.92,
        },
        {
            "title": "Parking enforcement",
            "url": "https://www.denvergov.org/Parking/Enforcement",
            "content": "Information about parking enforcement in Denver...",
            "score": 0.84,
        },
    ])
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=raw)):
        result = await search_denvergov_for_query("how do I pay a parking ticket")

    assert isinstance(result, DenverGovSearchResult)
    assert result.error is None
    assert len(result.results) == 2
    assert result.results[0].title == "Pay a parking ticket"
    assert result.results[0].url.startswith("https://www.denvergov.org/")
    assert "parking ticket" in result.results[0].snippet


@pytest.mark.asyncio
async def test_search_caches_by_normalized_query():
    raw = _tavily_response([{"title": "X", "url": "https://denvergov.org/x", "content": "x"}])
    mock_client = _mock_client(return_value=raw)
    with patch.object(denvergov_search, "_build_tavily_client", return_value=mock_client):
        await search_denvergov_for_query("How do I pay a parking ticket?")
        await search_denvergov_for_query("How do I pay a parking ticket?")

    # Underlying Tavily call should only have happened once thanks to caching.
    assert mock_client.search.call_count == 1


@pytest.mark.asyncio
async def test_search_normalizes_cache_key():
    """Whitespace and case shouldn't trigger a fresh Tavily call."""
    raw = _tavily_response([{"title": "X", "url": "https://denvergov.org/x", "content": "x"}])
    mock_client = _mock_client(return_value=raw)
    with patch.object(denvergov_search, "_build_tavily_client", return_value=mock_client):
        await search_denvergov_for_query("foo bar")
        await search_denvergov_for_query("  FOO BAR  ")
        await search_denvergov_for_query("Foo Bar")

    assert mock_client.search.call_count == 1


@pytest.mark.asyncio
async def test_search_returns_error_on_exception():
    """Tavily failures must surface as an `error` field, never as a raise —
    the tool_agent loop expects every tool to return a dict."""
    with patch.object(
        denvergov_search,
        "_build_tavily_client",
        return_value=_mock_client(side_effect=RuntimeError("Tavily down")),
    ):
        result = await search_denvergov_for_query("anything")

    assert result.error == "Tavily down"
    assert result.results == []


@pytest.mark.asyncio
async def test_search_empty_results_when_tavily_returns_none():
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=None)):
        result = await search_denvergov_for_query("nothing matches this")
    assert result.error is None
    assert result.results == []


@pytest.mark.asyncio
async def test_search_passes_constraint_args_to_tavily():
    """Verify max_results + include_domains are forwarded — the include
    domains pin is critical (we never want results from outside denvergov.org)."""
    mock_client = _mock_client(return_value=_tavily_response([]))
    with patch.object(denvergov_search, "_build_tavily_client", return_value=mock_client):
        await search_denvergov_for_query("test", max_results=5)

    mock_client.search.assert_awaited_once()
    kwargs = mock_client.search.await_args.kwargs
    assert kwargs["max_results"] == 5
    assert kwargs["include_domains"] == ["denvergov.org"]
    assert kwargs["query"] == "test"


@pytest.mark.asyncio
async def test_skips_hits_missing_url():
    raw = _tavily_response([
        {"title": "Has URL", "url": "https://denvergov.org/a", "content": "a"},
        {"title": "Missing URL", "url": "", "content": "b"},
        {"title": "Also missing", "content": "c"},  # no url key at all
    ])
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=raw)):
        result = await search_denvergov_for_query("query")

    assert len(result.results) == 1
    assert result.results[0].url == "https://denvergov.org/a"


@pytest.mark.asyncio
async def test_snippet_truncated_to_max_chars():
    long_content = "x" * 1000
    raw = _tavily_response([{"title": "T", "url": "https://denvergov.org/t", "content": long_content}])
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=raw)):
        result = await search_denvergov_for_query("query")

    assert len(result.results[0].snippet) == denvergov_search.SNIPPET_MAX_CHARS


@pytest.mark.asyncio
async def test_different_max_results_use_separate_cache_entries():
    """Cache key includes max_results so a 3-result and a 5-result lookup of
    the same query don't cross-contaminate."""
    mock_client = _mock_client(return_value=_tavily_response([]))
    with patch.object(denvergov_search, "_build_tavily_client", return_value=mock_client):
        await search_denvergov_for_query("same query", max_results=3)
        await search_denvergov_for_query("same query", max_results=5)

    assert mock_client.search.call_count == 2


@pytest.mark.asyncio
async def test_score_field_passed_through_when_present():
    raw = _tavily_response([{"title": "T", "url": "https://denvergov.org/t", "content": "c", "score": 0.77}])
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=raw)):
        result = await search_denvergov_for_query("q")
    assert result.results[0].score == 0.77


@pytest.mark.asyncio
async def test_score_field_none_when_missing():
    raw = _tavily_response([{"title": "T", "url": "https://denvergov.org/t", "content": "c"}])
    with patch.object(denvergov_search, "_build_tavily_client", return_value=_mock_client(return_value=raw)):
        result = await search_denvergov_for_query("q")
    assert result.results[0].score is None
