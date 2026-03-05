# Denver Open Data RAG — Architecture Decisions

## Graph State (`app/graph/state.py`)

### `messages: Annotated[list, add_messages]`
Uses LangGraph's `add_messages` reducer so that parallel branches can each append to the conversation without overwriting each other. The generator appends the AI response as an `AIMessage`; the scraper can optionally append a tool message. Without this reducer, whichever branch writes last would clobber the other.

### `needs_scrape: bool`
Set by the `intent_router` node (NOT the grader). The grader only sets `docs_relevant`. When `True`, the intent router fans out into two parallel branches: `generate` and `scraper`. The intent router combines two signals: `has_layers=True` in the retrieved doc metadata AND the query semantically requires field-level detail (e.g., "what fields are available", "show me the map").

### `map_viewer_url: str | None`
Kept as a top-level key (not nested inside `scraped_layer_data`) so the FastAPI SSE layer can detect it cleanly. When LangGraph emits a state update containing this key, the streaming endpoint emits `event: map_viewer` instead of `event: token`, which the frontend catches to render a Map Viewer button or iframe.

### `scraped_layer_data: dict | None`
Written only by the scraper branch. The generator node never reads this — it starts streaming immediately using only `retrieved_docs`. This eliminates any read/write race condition between the two parallel branches.

### `retry_count: int`
Caps query rewrite retries at 2. The grader routes to a `rewrite` node when `docs_relevant=False`; the rewrite node increments this counter. If `retry_count >= 2`, the graph routes to `generate` regardless, to avoid an infinite loop.

---

## Parallel Streaming Pattern (Option A — Concurrent Streaming)

```
Grader
  |
  ├─── [needs_scrape=True] ──► generate (streams tokens immediately)
  │                        ──► scraper (fetches layer data + map URL)
  │
  └─── [needs_scrape=False] ─► generate
```

### Why Option A over Option B (wait-then-generate)
The user sees LLM tokens immediately rather than waiting for the scraper to finish (ArcGIS HTML parsing can be slow). The LLM is prompted to hedge with something like "I'm pulling up the live map viewer for you now..." so the UX feels intentional rather than incomplete.

### SSE Event Types
- `event: token` — streaming LLM text chunk
- `event: map_viewer` — emitted by FastAPI when `map_viewer_url` lands in state; payload is the URL; frontend renders Map Viewer button/iframe

### Hedge System Prompt
The generator node's system prompt should instruct the LLM: *"If the query has been sent to the map scraper tool, acknowledge it in your response (e.g., 'I'm pulling up the live map viewer for you now...') while you summarize the retrieved data."*

---

## Routing Logic Summary

```
retrieve → grade →
  docs_relevant=False, retry_count < 2  → rewrite → retrieve
  docs_relevant=False, retry_count >= 2 → generate (graceful fallback)
  docs_relevant=True, needs_scrape=False → generate
  docs_relevant=True, needs_scrape=True  → generate ─┐  (parallel)
                                          → scraper  ─┘
```

---

## Full Node Map

| Node | Sets in State | Notes |
|------|---------------|-------|
| `main_router` | nothing | pass-through; explicit extensibility hook for future data sources |
| `retriever` | `retrieved_docs` | `QdrantVectorStore.from_existing_collection`, HYBRID mode, k=5 |
| `grader` | `docs_relevant` | LLM structured output (`GraderOutput: relevant: bool`); sets `docs_relevant` only |
| `rewriter` | `query`, `retry_count` | LLM rewrites query; increments `retry_count` |
| `intent_router` | `needs_scrape` | LLM intent classification + `has_layers` metadata check; sets `needs_scrape` |
| `generator` | `messages` | async LLM; standard or hedge prompt based on `needs_scrape` |
| `scraper` | `scraped_layer_data`, `map_viewer_url` | async httpx; scrapes first `has_layers` doc only; targets `id="viewInSection"` |

---

## Conditional Edge Functions

```python
def route_after_grader(state) -> str:
    if not state["docs_relevant"]:
        return "rewrite" if state["retry_count"] < 2 else "generate"
    return "intent_router"

def route_after_intent(state) -> list[str]:
    return ["generate", "scraper"] if state["needs_scrape"] else ["generate"]
```

---

## File Structure

```
app/
  main.py
  graph/
    state.py
    orchestrator.py
    nodes/
      router.py, retriever.py, grader.py, intent_router.py,
      generator.py, scraper.py, rewriter.py
  prompts/
    grader_prompt.py, intent_prompt.py, generator_prompt.py, rewriter_prompt.py
```

---

## Scraper Scope Decision

Scraper targets the **first** retrieved doc with `has_layers=True` only (MVP simplicity). Future iterations can fan out across multiple docs.

---

## FastAPI SSE Pattern

Uses `graph.astream_events(version="v2")`. The `langgraph_node` field in event metadata distinguishes generator tokens from scraper output:
- `on_chat_model_stream` from `generate` node → `event: token`
- `on_chain_end` from `scraper` node with `map_viewer_url` → `event: map_viewer`

---

## ArcGIS Scraper URL Pattern
- Base URL from Qdrant metadata: `{base_url}/{layer_id}/query?...`
- Map viewer link: found in `id="viewInSection"` in the child layer HTML page
- Fields: available in the child layer `fields` property
- These are fetched dynamically at query time — not stored in the vector index
