# LangGraph Agent — Implementation Reference

## Graph Overview

```
START → main_router → retriever → grader
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                 ▼
                rewriter         generate          intent_router
                    │               │                  │
                    └──→ retriever  END      ┌─────────┴──────────┐
                                             ▼                    ▼
                                          generate             scraper
                                             │                    │
                                            END                  END
```

**Entry:** `START → main_router → retriever`

**Conditional edges:**

| From | Condition | To |
|---|---|---|
| `grader` | `docs_relevant=False` AND `retry_count < 2` | `rewriter` |
| `grader` | `docs_relevant=False` AND `retry_count >= 2` | `generate` |
| `grader` | `docs_relevant=True` | `intent_router` |
| `intent_router` | `needs_scrape=True` | `generate` + `scraper` (parallel fan-out) |
| `intent_router` | `needs_scrape=False` | `generate` |

---

## State — `AgentState` (`app/graph/state.py`)

| Field | Type | Description |
|---|---|---|
| `query` | `str` | Current query string (may be rewritten on retry) |
| `messages` | `list` | Conversation history; uses `add_messages` reducer (append-only) |
| `retrieved_docs` | `list[Document]` | Docs returned from Qdrant hybrid search |
| `docs_relevant` | `bool \| None` | Grader output |
| `needs_scrape` | `bool` | Set by intent_router; triggers parallel scraper fan-out |
| `retry_count` | `int` | Tracks rewrite retries; max 2 |
| `scraped_layer_data` | `dict \| None` | Structured layer info from ArcGIS scrape |
| `map_viewer_url` | `str \| None` | ArcGIS map viewer link; emitted as SSE `map_viewer` event |

---

## Nodes

### `main_router` (`nodes/router.py`)
Pass-through no-op at graph entry. Placeholder for future multi-source dispatch or query classification.

### `retriever` (`nodes/retriever.py`)
Hybrid vector search against Qdrant.
- **Dense:** `GoogleGenerativeAIEmbeddings` (`models/gemini-embedding-001`)
- **Sparse:** `FastEmbedSparse` (`Qdrant/bm25`)
- **Mode:** `RetrievalMode.HYBRID`
- **Top-k:** 5
- Uses `@lru_cache(maxsize=1)` singleton to avoid reconnecting per call.
- Config: `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION_NAME`

### `grader` (`nodes/grader.py`)
LLM relevance classifier with structured output.
- Returns `GraderOutput(relevant: bool)`
- `temperature=0`
- Formats docs as `[service_name]\n{page_content}` blocks

### `intent_router` (`nodes/intent_router.py`)
Decides whether to trigger the scraper.
1. Short-circuits to `needs_scrape=False` if no retrieved doc has `has_layers=True`
2. Otherwise calls LLM with `IntentOutput(needs_map: bool)` to check if the query needs map/field-level detail
- `temperature=0`

### `rewriter` (`nodes/rewriter.py`)
Rewrites the query to improve retrieval, increments `retry_count`.
- `temperature=0.3`
- Returns `{query: <rewritten>, retry_count: +1}`

### `generator` (`nodes/generator.py`)
Async streaming LLM response.
- Uses `GENERATOR_SYSTEM_HEDGE` prompt when `needs_scrape=True` (scraper running concurrently), `GENERATOR_SYSTEM_STANDARD` otherwise
- `temperature=0.2`, `streaming=True`
- Formats docs as `[service_name]\nURL: {base_url}\n\n{page_content}` blocks
- Returns `{messages: [AIMessage]}`

### `scraper` (`nodes/scraper.py`)
Async ArcGIS HTML scraper. Runs in parallel with `generator` when `needs_scrape=True`.
1. Finds first `retrieved_docs` entry with `has_layers=True`
2. Parses `full_metadata` JSON for `layer_id` and `fields`
3. Fetches `{base_url}/{layer_id}` HTML page via `httpx`
4. Extracts map viewer URL from `#viewInSection > a[href]`
5. Returns `scraped_layer_data` dict + `map_viewer_url`
- Timeout: 10s. Failures are logged as warnings; returns `None` values gracefully.

---

## SSE Events (streamed by `app/main.py`)

| Event | Payload | Trigger |
|---|---|---|
| `token` | `{"text": "<chunk>"}` | `on_chat_model_stream` from `generate` node |
| `map_viewer` | `{"url": "<arcgis_url>"}` | `on_chain_end` from `scraper` node (if `map_viewer_url` present) |
| `error` | `{"error": "<msg>"}` | Exception during streaming |
| `done` | `{}` | Stream complete |

---

## Config (env vars)

| Var | Default | Used By |
|---|---|---|
| `GEMINI_API_KEY` | — | All LLM nodes |
| `GEMINI_MODEL` | `gemini-2.5-flash` | All LLM nodes |
| `QDRANT_URL` | `http://localhost:6333` | retriever, ingest |
| `QDRANT_API_KEY` | `None` | retriever, ingest (required for Qdrant Cloud) |
| `QDRANT_COLLECTION_NAME` | `denver_gis_catalog` | retriever, ingest |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://localhost:5173` | FastAPI CORS middleware |
