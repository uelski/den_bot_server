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
        "needs_pg_query": False,
        "pg_table": None,
        "pg_query_results": None,
        "retry_count": 0,
        "scraped_layer_data": None,
        "map_viewer_urls": [],
    }

    async def event_stream():
        try:
            async for event in graph.astream_events(initial_state, version="v2"):
                event_type = event.get("event", "")
                metadata = event.get("metadata", {})
                node = metadata.get("langgraph_node", "")

                # Generator token stream
                if event_type == "on_chat_model_stream" and node == "generate":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        payload = json.dumps({"text": chunk.content})
                        yield f"event: token\ndata: {payload}\n\n"

                # Retriever finished — emit sources
                elif event_type == "on_chain_end" and node == "retriever":
                    output = event.get("data", {}).get("output", {})
                    docs = output.get("retrieved_docs", []) if output else []
                    sources = [
                        {
                            "service_name": d.metadata.get("service_name"),
                            "base_url": d.metadata.get("base_url"),
                            **({"hub_url": d.metadata["hub_url"]} if d.metadata.get("hub_url") else {}),
                        }
                        for d in docs
                        if d.metadata.get("base_url")
                    ]
                    if sources:
                        payload = json.dumps({"sources": sources})
                        yield f"event: sources\ndata: {payload}\n\n"

                    # Emit hub_urls as map_viewer links (scraper may add more later)
                    hub_links = [
                        {
                            "url": d.metadata["hub_url"].rstrip("/").removesuffix("/about"),
                            "label": f"View {d.metadata.get('service_name', 'data')} map",
                        }
                        for d in docs
                        if d.metadata.get("hub_url")
                    ]
                    if hub_links:
                        payload = json.dumps({"urls": hub_links})
                        yield f"event: map_viewer\ndata: {payload}\n\n"

                # Postgres query finished — emit row count
                elif event_type == "on_chain_end" and node == "pg_query":
                    output = event.get("data", {}).get("output", {})
                    if not isinstance(output, dict):
                        continue
                    results = output.get("pg_query_results")
                    if results is not None:
                        payload = json.dumps({"row_count": len(results), "status": "complete"})
                        yield f"event: pg_data\ndata: {payload}\n\n"

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
