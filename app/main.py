"""main.py — FastAPI SSE streaming endpoint."""

import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.graph.orchestrator import graph

logger = logging.getLogger(__name__)

app = FastAPI(title="Denver Open Data RAG")

origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryBody(BaseModel):
    query: str


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


def build_map_viewer_links(docs) -> list[dict]:
    """Build the deduplicated `map_viewer` SSE link list from retrieved docs.

    Dedup key: the normalized URL (after stripping trailing slash and `/about`).
    The first doc contributing a given URL wins the label.
    """
    seen: set[str] = set()
    links: list[dict] = []
    for d in docs:
        hub_url = d.metadata.get("hub_url")
        if not hub_url:
            continue
        url = hub_url.rstrip("/").removesuffix("/about")
        if url in seen:
            continue
        seen.add(url)
        links.append({
            "url": url,
            "label": f"View {d.metadata.get('service_name', 'data')} map",
        })
    return links


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/query")
async def query_endpoint(body: QueryBody):
    initial_state = {
        "query": body.query,
        "messages": [],
        "requires_rag": True,
        "retrieved_docs": [],
        "docs_relevant": None,
        "needs_scrape": False,
        "retry_count": 0,
        "scraped_layer_data": None,
        "map_viewer_urls": [],
    }

    # LangSmith tags (no-op when tracing disabled): make runs searchable by
    # route (api-query) and include a short preview of the query in the run name.
    query_preview = body.query[:40].replace("\n", " ")
    run_config = {
        "run_name": f"/query: {query_preview}",
        "tags": ["api-query"],
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
