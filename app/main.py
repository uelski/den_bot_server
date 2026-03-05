"""main.py — FastAPI SSE streaming endpoint."""

import json
import logging

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.graph.orchestrator import graph

logger = logging.getLogger(__name__)

app = FastAPI(title="Denver Open Data RAG")


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
        "retrieved_docs": [],
        "docs_relevant": None,
        "needs_scrape": False,
        "retry_count": 0,
        "scraped_layer_data": None,
        "map_viewer_url": None,
    }

    async def event_stream():
        try:
            async for event in graph.astream_events(initial_state, version="v2"):
                event_name = event.get("name", "")
                event_type = event.get("event", "")
                metadata = event.get("metadata", {})
                node = metadata.get("langgraph_node", "")

                # Generator token stream
                if event_type == "on_chat_model_stream" and node == "generate":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        payload = json.dumps({"text": chunk.content})
                        yield f"event: token\ndata: {payload}\n\n"

                # Scraper finished — emit map_viewer_url if present
                elif event_type == "on_chain_end" and node == "scraper":
                    output = event.get("data", {}).get("output", {})
                    map_url = output.get("map_viewer_url") if output else None
                    if map_url:
                        payload = json.dumps({"url": map_url})
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
