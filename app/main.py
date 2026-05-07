"""main.py — FastAPI SSE streaming endpoint."""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Route app-level INFO logs to stdout. uvicorn configures its own loggers
# (uvicorn / uvicorn.error / uvicorn.access) but doesn't touch root or
# app loggers, so without this our lifespan / startup messages are
# silently dropped at WARNING-level filtering. basicConfig is a no-op
# if a handler is already attached, so it's safe under reload too.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app.graph.memory import build_checkpointer
from app.graph.orchestrator import build_graph, graph as stateless_graph
from app.tools.denvergov_search import DOC_TYPE as DENVERGOV_SEARCH_DOC_TYPE

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI startup/shutdown hook. Builds the AsyncRedisSaver once
    (when REDIS_URL is set) and compiles a memory-enabled graph backed by
    it. Both are stored on app.state so request handlers can reach them
    without re-creating per-request. When REDIS_URL is unset we fall
    through to the stateless graph imported at module load — endpoints
    work fine, just without cross-turn memory."""
    checkpointer = await build_checkpointer()
    if checkpointer is not None:
        app.state.graph = build_graph(checkpointer=checkpointer)
        app.state.checkpointer = checkpointer
        app.state.memory_enabled = True
        logger.info("Compiled memory-enabled graph with AsyncRedisSaver")
    else:
        app.state.graph = stateless_graph
        app.state.checkpointer = None
        app.state.memory_enabled = False
        logger.info("Compiled stateless graph (no REDIS_URL set)")
    yield


app = FastAPI(title="Denver Open Data RAG", lifespan=lifespan)

origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryBody(BaseModel):
    query: str
    # Optional client-supplied thread identifier. When present and a Redis
    # checkpointer is configured, prior turns under this thread_id are
    # loaded and the new turn is appended. When absent (or no checkpointer),
    # the request runs single-turn with a throwaway thread id.
    thread_id: str | None = None


def build_sources_payload(docs) -> list[dict]:
    """Build the deduplicated `sources` SSE payload from retrieved docs.

    Dedup key: (service_name, base_url, hub_url, neighborhood_name). Neighborhood
    demographics docs keep per-neighborhood entries; catalog docs collapse to one.
    """
    seen: set[tuple] = set()
    sources: list[dict] = []
    for d in docs:
        if not d.metadata.get("base_url"):
            continue
        service_name = d.metadata.get("service_name")
        base_url = d.metadata.get("base_url")
        hub_url = d.metadata.get("hub_url")
        is_neighborhood = d.metadata.get("doc_type") == "neighborhood_demographics"
        neighborhood_name = d.metadata.get("neighborhood_name") if is_neighborhood else None
        key = (service_name, base_url, hub_url, neighborhood_name)
        if key in seen:
            continue
        seen.add(key)
        entry: dict = {"service_name": service_name, "base_url": base_url}
        if hub_url:
            entry["hub_url"] = hub_url
        if neighborhood_name:
            entry["neighborhood_name"] = neighborhood_name
            entry["doc_type"] = "neighborhood_demographics"
        sources.append(entry)
    return sources


def _extract_weather_fields(output: dict) -> dict:
    return {
        "neighborhood_name": output.get("neighborhood_name"),
        "lat": output.get("lat"),
        "lon": output.get("lon"),
        "period_count": (
            len(output["periods"]) if isinstance(output.get("periods"), list) else None
        ),
    }


def _extract_alerts_fields(output: dict) -> dict:
    sample = None
    if isinstance(output.get("alerts"), list):
        sample = [
            {
                "header": (a.get("header") or "")[:160],
                "effect": a.get("effect"),
                "affected_routes": a.get("affected_routes") or [],
                "affected_stops": a.get("affected_stops") or [],
            }
            for a in output["alerts"][:2]
        ]
    return {
        "total_active": output.get("total_active"),
        "filtered_count": output.get("filtered_count"),
        "alerts_url": output.get("alerts_url"),
        "sample": sample,
    }


def _extract_arrivals_fields(output: dict) -> dict:
    matched_stops = output.get("matched_stops") or []
    arrivals = output.get("arrivals") or []
    sample = [
        {
            "route_short_name": a.get("route_short_name"),
            "headsign": a.get("headsign"),
            "minutes_until": a.get("minutes_until"),
            "stop_name": a.get("stop_name"),
        }
        for a in arrivals[:3]
    ]
    return {
        "stop_count": len(matched_stops),
        "arrival_count": len(arrivals),
        "resolution_method": output.get("resolution_method"),
        "has_realtime": bool(output.get("has_realtime")),
        "sample": sample,
    }


def _extract_vehicles_fields(output: dict) -> dict:
    matched_route = output.get("matched_route") or {}
    vehicles = output.get("vehicles") or []
    return {
        "route_short_name": matched_route.get("short_name"),
        "route_long_name": matched_route.get("long_name"),
        "vehicle_count": len(vehicles),
        "has_realtime": bool(output.get("has_realtime")),
    }


def _extract_denvergov_search_fields(output: dict) -> dict:
    results = output.get("results") or []
    return {
        "query": output.get("query"),
        "result_count": len(results),
        "sample": [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": (r.get("snippet") or "")[:200],
            }
            for r in results[:2]
        ],
    }


# Per-tool field schemas. Every payload for a known tool always emits its full
# keyset (with null for missing values) so the frontend can rely on a fixed
# discriminated-union shape keyed on `tool`.
_TOOL_SCHEMAS: dict[str, list[str]] = {
    "get_neighborhood_weather": ["neighborhood_name", "lat", "lon", "period_count"],
    "get_rtd_service_alerts": ["total_active", "filtered_count", "alerts_url", "sample"],
    "get_rtd_next_arrivals": [
        "stop_count", "arrival_count", "resolution_method", "has_realtime", "sample",
    ],
    "get_rtd_vehicle_positions": [
        "route_short_name", "route_long_name", "vehicle_count", "has_realtime",
    ],
    "search_denver_gov": ["query", "result_count", "sample"],
}
_TOOL_EXTRACTORS = {
    "get_neighborhood_weather": _extract_weather_fields,
    "get_rtd_service_alerts": _extract_alerts_fields,
    "get_rtd_next_arrivals": _extract_arrivals_fields,
    "get_rtd_vehicle_positions": _extract_vehicles_fields,
    "search_denver_gov": _extract_denvergov_search_fields,
}


def _summarize_tool_output(tool_name: str, output) -> dict:
    """Build the SSE `tool_result` payload body for a tool invocation.

    Returns a deterministic shape per `tool_name`: every key in the tool's
    schema is always present (`null` when the underlying value is missing or
    when the tool failed). Frontend code can branch once on `tool` and rely
    on the keyset thereafter — no existence checks needed.

    Unknown tools fall back to the minimal `{ok, error}` shape.
    """
    schema = _TOOL_SCHEMAS.get(tool_name, [])
    base: dict = {"ok": True, "error": None}
    base.update({k: None for k in schema})

    if output is None:
        return {**base, "ok": False, "error": "no output"}

    # LangChain wraps tool returns in a ToolMessage on .output sometimes; unwrap.
    if hasattr(output, "content"):
        try:
            import json as _json
            content = output.content
            if isinstance(content, str):
                content = _json.loads(content) if content.strip().startswith("{") else content
            output = content
        except (ValueError, TypeError):
            pass

    if not isinstance(output, dict):
        # Non-dict success: ok=True, all fields null.
        return base

    if output.get("error"):
        return {**base, "ok": False, "error": output["error"]}

    extractor = _TOOL_EXTRACTORS.get(tool_name)
    if extractor:
        try:
            base.update(extractor(output))
        except Exception as exc:
            logger.warning("failed to summarize %s output: %s", tool_name, exc)

    return base


def build_map_viewer_links(docs) -> list[dict]:
    """Build the deduplicated `map_viewer` SSE link list from retrieved docs.

    URL: prefer per-entity `map_url` (parks Google Maps deep link, RTD
    NextRide stop/route URL); fall back to `hub_url` for catalog and
    neighborhood docs that don't set `map_url`. Label: prefer per-entity
    `display_name`; fall back to `service_name`. Dedup key: the normalized
    URL (after stripping trailing slash and `/about`).
    """
    seen: set[str] = set()
    links: list[dict] = []
    for d in docs:
        url_raw = d.metadata.get("map_url") or d.metadata.get("hub_url")
        if not url_raw:
            continue
        url = url_raw.rstrip("/").removesuffix("/about")
        if url in seen:
            continue
        seen.add(url)
        label_name = d.metadata.get("display_name") or d.metadata.get("service_name", "data")
        links.append({
            "url": url,
            "label": f"View {label_name} map",
        })
    return links


# Cap on tool-derived map_viewer URLs surfaced to the frontend. Keeps the
# external-link panel compact when, for example, a multi-platform station
# resolves to 4+ paired stops plus distinct routes.
_TOOL_MAP_VIEWER_LINK_CAP = 4


def _arrivals_map_viewer_links(output: dict) -> list[dict]:
    """Pull NextRide stop URLs (paired directions) from a get_rtd_next_arrivals
    tool output. Stop labels combine name + desc per design decision."""
    links: list[dict] = []
    for stop in output.get("matched_stops") or []:
        url = stop.get("nextride_url")
        if not url:
            continue
        name = (stop.get("stop_name") or "").strip()
        desc = (stop.get("stop_desc") or "").strip()
        if name and desc:
            label = f"{name} — {desc}"
        else:
            label = name or desc or "RTD stop"
        links.append({"url": url, "label": label})
    return links


def _vehicles_map_viewer_links(output: dict) -> list[dict]:
    """Pull the matched route's NextRide URL from a get_rtd_vehicle_positions
    tool output. Single link per call (one route per query)."""
    matched = output.get("matched_route") or {}
    url = matched.get("nextride_url")
    if not url:
        return []
    short = (matched.get("short_name") or "").strip()
    long_name = (matched.get("long_name") or "").strip()
    if short and long_name:
        label = f"{short} — {long_name}"
    else:
        label = short or long_name or "RTD route"
    return [{"url": url, "label": label}]


_TOOL_MAP_VIEWER_BUILDERS = {
    "get_rtd_next_arrivals": _arrivals_map_viewer_links,
    "get_rtd_vehicle_positions": _vehicles_map_viewer_links,
}


def _denvergov_search_sources(output: dict) -> list[dict]:
    """Build `sources` SSE entries from a search_denver_gov tool output —
    one entry per Tavily hit. Each entry uses the page title as
    `service_name` (the label the frontend renders) and the URL as both
    `base_url` and `hub_url` (the frontend uses `hub_url` as the link).
    Stamps `doc_type = "denvergov_search_result"` so the frontend can
    distinguish Tavily-derived links from retrieval-driven catalog sources.
    Dedup + skip-on-error handled by build_tool_sources."""
    sources: list[dict] = []
    for hit in output.get("results") or []:
        url = hit.get("url")
        if not url:
            continue
        title = (hit.get("title") or "").strip() or "denvergov.org page"
        sources.append({
            "service_name": title,
            "base_url": url,
            "hub_url": url,
            "doc_type": DENVERGOV_SEARCH_DOC_TYPE,
        })
    return sources


# Per-tool builders for the `sources` SSE event. Distinct from the
# map_viewer builders: source URLs are citation-shaped (link + label
# rendered as a list of references), not map-shaped. Use this when a
# tool surfaces external pages that aren't maps.
_TOOL_SOURCES_BUILDERS = {
    "search_denver_gov": _denvergov_search_sources,
}


def build_tool_map_viewer_links(tool_name: str, output) -> list[dict]:
    """Build the `map_viewer` SSE payload from a tool's structured return.

    Mirrors `build_map_viewer_links` for the tool path. Per-tool dispatch via
    `_TOOL_MAP_VIEWER_BUILDERS`; URL-deduped; capped at the top
    `_TOOL_MAP_VIEWER_LINK_CAP` unique entries (first-seen label wins). Returns
    [] when the tool didn't produce links or its output isn't a dict.
    """
    builder = _TOOL_MAP_VIEWER_BUILDERS.get(tool_name)
    if builder is None:
        return []

    if hasattr(output, "content"):
        try:
            content = output.content
            if isinstance(content, str) and content.strip().startswith("{"):
                content = json.loads(content)
            output = content
        except (ValueError, TypeError):
            pass
    if not isinstance(output, dict):
        return []
    if output.get("error"):
        return []

    seen: set[str] = set()
    capped: list[dict] = []
    for link in builder(output):
        url = link.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        capped.append(link)
        if len(capped) >= _TOOL_MAP_VIEWER_LINK_CAP:
            break
    return capped


def build_tool_sources(tool_name: str, output) -> list[dict]:
    """Build the `sources` SSE payload from a tool's structured return.

    Mirrors `build_sources_payload` for the tool path. Per-tool dispatch via
    `_TOOL_SOURCES_BUILDERS`; deduped on `(service_name, base_url, hub_url)`.
    Returns [] when the tool didn't produce sources or its output isn't a dict.
    """
    builder = _TOOL_SOURCES_BUILDERS.get(tool_name)
    if builder is None:
        return []

    if hasattr(output, "content"):
        try:
            content = output.content
            if isinstance(content, str) and content.strip().startswith("{"):
                content = json.loads(content)
            output = content
        except (ValueError, TypeError):
            pass
    if not isinstance(output, dict):
        return []
    if output.get("error"):
        return []

    seen: set[tuple] = set()
    deduped: list[dict] = []
    for entry in builder(output):
        key = (
            entry.get("service_name"),
            entry.get("base_url"),
            entry.get("hub_url"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/query")
async def query_endpoint(body: QueryBody, request: Request):
    # Per-turn fields reset to defaults — they overwrite whatever the
    # checkpointer loaded from prior turns. Note: `messages` is intentionally
    # OMITTED from initial_state. With the add_messages reducer, passing []
    # is technically a no-op merge, but excluding the key entirely is the
    # safer pattern — it lets the loaded checkpoint state's messages flow
    # through untouched, with no chance of an accidental reset.
    initial_state = {
        "query": body.query,
        "requires_rag": True,
        "retrieved_docs": [],
        "docs_relevant": None,
        "needs_scrape": False,
        "needs_tool": False,
        "tool_results": None,
        "retry_count": 0,
        "scraped_layer_data": None,
        "map_viewer_urls": [],
    }

    # When the client doesn't send a thread_id, generate a throwaway one
    # so the LangGraph checkpointer (when configured) has a key to write
    # under. Throwaway threads fall out of Redis on TTL expiry.
    thread_id = body.thread_id or str(uuid.uuid4())
    graph = request.app.state.graph

    # LangSmith tags (no-op when tracing disabled): make runs searchable by
    # route (api-query) and include a short preview of the query in the run name.
    # `configurable.thread_id` keys the LangGraph checkpointer; required even
    # when no checkpointer is attached (LangGraph ignores it cleanly in that case).
    query_preview = body.query[:40].replace("\n", " ")
    run_config = {
        "run_name": f"/query: {query_preview}",
        "tags": ["api-query"],
        "configurable": {"thread_id": thread_id},
    }

    async def event_stream():
        try:
            async for event in graph.astream_events(initial_state, version="v2", config=run_config):
                event_type = event.get("event", "")
                metadata = event.get("metadata", {})
                node = metadata.get("langgraph_node", "")

                # Generator token stream
                if event_type == "on_chat_model_stream" and node == "generate":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        payload = json.dumps({"text": chunk.content})
                        yield f"event: token\ndata: {payload}\n\n"

                # Retriever finished — emit sources + map_viewer
                elif event_type == "on_chain_end" and node == "retriever":
                    output = event.get("data", {}).get("output", {})
                    docs = output.get("retrieved_docs", []) if output else []

                    sources = build_sources_payload(docs)
                    if sources:
                        yield f"event: sources\ndata: {json.dumps({'sources': sources})}\n\n"

                    hub_links = build_map_viewer_links(docs)
                    if hub_links:
                        yield f"event: map_viewer\ndata: {json.dumps({'urls': hub_links})}\n\n"

                # Tool starting inside tool_agent — let the frontend show "looking up..." UI
                elif event_type == "on_tool_start" and node == "tool_agent":
                    tool_name = event.get("name", "")
                    tool_input = event.get("data", {}).get("input") or {}
                    payload = json.dumps({
                        "tool": tool_name,
                        "status": "running",
                        "args": tool_input,
                    })
                    yield f"event: tool_call\ndata: {payload}\n\n"

                # Tool finished — frontend can transition to "got data, generating..."
                elif event_type == "on_tool_end" and node == "tool_agent":
                    tool_name = event.get("name", "")
                    output = event.get("data", {}).get("output")
                    summary = _summarize_tool_output(tool_name, output)
                    payload = json.dumps({
                        "tool": tool_name,
                        "status": "complete",
                        **summary,
                    })
                    yield f"event: tool_result\ndata: {payload}\n\n"

                    # Surface NextRide deep links from arrivals/vehicles tools
                    # via the same map_viewer event the retrieval path emits.
                    tool_links = build_tool_map_viewer_links(tool_name, output)
                    if tool_links:
                        yield f"event: map_viewer\ndata: {json.dumps({'urls': tool_links})}\n\n"

                    # Surface citation-shaped URLs (e.g. denvergov.org pages
                    # from search_denver_gov) via the same sources event the
                    # retrieval path emits — frontend renders these as a
                    # references list, not a map panel.
                    tool_sources = build_tool_sources(tool_name, output)
                    if tool_sources:
                        yield f"event: sources\ndata: {json.dumps({'sources': tool_sources})}\n\n"

                # Scraper finished — emit scraped map_viewer_url if present
                elif event_type == "on_chain_end" and node == "scraper":
                    output = event.get("data", {}).get("output", {})
                    scraped_data = output.get("scraped_layer_data") if output else None
                    map_urls = output.get("map_viewer_urls", []) if output else []
                    if map_urls:
                        service_name = scraped_data.get("service_name", "data") if scraped_data else "data"
                        scraped_links = [
                            {"url": url, "label": f"View {service_name} map"}
                            for url in map_urls
                        ]
                        payload = json.dumps({"urls": scraped_links})
                        yield f"event: map_viewer\ndata: {payload}\n\n"

        except Exception as exc:
            logger.exception("Streaming error: %s", exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
