# Next steps

Living priority list for the Denver Open Data RAG project. Ordered by what delivers the most leverage for the least effort first. Scope of each item is intentionally loose — refine when picking something up.

## 1. LangSmith observability (next)

Wire up LangSmith to get node-by-node traces, LLM call inputs/outputs, token counts, and latency breakdowns with near-zero code changes.

**Why now**: we keep asking questions like "which node is slow?" and "did the generator actually see the hub_url?" — those are free with tracing. Also foundational for #2 (testing) because LangSmith captures runs that can be promoted into eval datasets.

**Tasks**:
- Sign up / create a LangSmith project, grab API key.
- Add `LANGCHAIN_API_KEY`, `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_PROJECT=denver-rag-dev` to `.env` (and `.env.example` if we create one).
- Invoke a query through `/query` and confirm the trace shows up in the LangSmith UI.
- Tag traces with `thread_id` and a short query hash so multi-turn sessions (coming in #3) are easy to find.
- Optional: add LangSmith `@traceable` decorators on any custom helper functions we want visible (e.g., the `_augment_with_fact_list` step in the ingest script — though that's offline).

**Effort**: ~30 min. Zero code changes if we stop at env vars.

## 2. Testing

No automated tests today. A thin pyramid that pays for itself:

**Node-level unit tests** (pytest + pytest-asyncio):
- `retriever.py` — mock Qdrant, verify top-k and metadata passthrough.
- `grader.py` — mock LLM, verify boolean output handling.
- `intent_router.py` — assert has_layers logic and needs_scrape setting.
- `generator.py` — mock the LLM streaming interface, verify prompt selection and `_format_docs` behavior for mixed neighborhood/catalog docs.
- `main.py` SSE dedup logic — feed synthetic retrieved_docs, assert sources and map_viewer event shapes.

**Integration tests**:
- End-to-end `graph.ainvoke()` with a deterministic mocked LLM and a real (or mocked) Qdrant — assert the graph takes the right path for `{general, retrieval-only, retrieval+scrape}` queries.

**Eval harness** (once LangSmith is wired):
- Curate ~20 representative queries with expected behaviors (hits neighborhood docs, hits catalog docs, general greeting, edge cases).
- Use LangSmith datasets to regression-test prompt/model changes.

**Effort**: unit tests ~half a day; integration ~half a day; eval harness incremental.

## 3. Memory (multi-turn conversations)

Today the graph is stateless per request — each `/query` starts from scratch. The frontend can't ask follow-up questions like "what about Five Points?" after a query about Capitol Hill.

**Tasks**:
- Add a LangGraph `Checkpointer` (start with `MemorySaver` for local dev, upgrade to `PostgresSaver` or `RedisSaver` later for prod).
- Accept a `thread_id` in the `/query` request body; pass it in `config={"configurable": {"thread_id": ...}}` when invoking the graph.
- The `messages` state field (already `Annotated[list, add_messages]`) will accumulate across turns automatically via the reducer.
- Update the generator prompt to treat prior messages as context, not just the latest query.
- Frontend: generate/manage a thread_id per chat session and send it with each query.

**Decision**: whether to persist memory across API restarts (`PostgresSaver`) or keep it in-process only (`MemorySaver`). In-process is fine for now; upgrade when we deploy.

**Effort**: ~half a day for in-process memory end-to-end including frontend changes.

## 4. Deployment

`deployment.md` already sketches a GCP Cloud Run + Qdrant Cloud path. This item is executing on it.

**Tasks**:
- Build + push the FastAPI container to Artifact Registry.
- Provision Qdrant Cloud (or self-host on GKE — decision point).
- Re-run `scripts/ingest.py` + `scripts/ingest_neighborhoods.py` against the prod Qdrant instance.
- Cloud Run service: set `QDRANT_URL`, `QDRANT_API_KEY`, `GEMINI_API_KEY`, `LANGCHAIN_*` as secrets.
- Tighten `ALLOWED_ORIGINS` from the local dev values to the real frontend host.
- Add a lightweight auth layer if the frontend isn't going to be our own trusted deployment.
- Rate limiting (token bucket on `/query`) — Gemini calls aren't free.

**Decision**: Qdrant Cloud vs self-hosted GKE. Qdrant Cloud is faster to ship; GKE is cheaper at scale and keeps data in our tenancy.

**Effort**: 1-2 days depending on GCP familiarity.

## 5. More data sources

The catalog today is Denver GIS services from ArcGIS FeatureServer. Plenty of other high-signal Denver open data to pull in.

**Candidates** (rough prioritization by agent utility):
- **311 call data** — "what are common complaints in Five Points" type queries. CSV/JSON from opendata-geospatialdenver.
- **Crime data** — Denver Police publishes incident-level data. High utility, needs thoughtful privacy framing.
- **Building permits / business licenses** — city-wide development signal.
- **ACS vintages beyond 2017-2021** — let users compare a neighborhood's demographics over time.
- **OSM / POI data** — parks, schools, transit stops. Could enrich neighborhood summaries.
- **Non-ArcGIS Denver sources** — the open data portal has CSVs that don't have FeatureServer backing; would need a separate ingestion path.

**Tasks** (per source):
- Write a small enrichment script that transforms the raw source into the same Document shape used for either the catalog or neighborhood-demographics pattern.
- Decide on chunking strategy (whole-record, per-section, per-incident, etc.).
- Tag with a distinct `doc_type` so retrieval filters and SSE rendering can branch appropriately (like `neighborhood_demographics` does).
- Add any source-specific `hub_url` logic.

**Decision**: each source roughly doubles the ingestion surface area. Start with one — probably 311 given its high query utility — and generalize patterns from there.

**Effort**: ~1 day per source for initial integration, less for subsequent ones as patterns solidify.

---

## Not on the list but worth noting

- **Hub URL audit** already has its own workflow in `scripts/audit_hub_urls.py` + `scripts/apply_hub_url_updates.py` — run periodically (quarterly?) or when broken links are reported. See `hub_url_audit_system.md` in memory.
- **Frontend adaptation** to the enriched `sources` SSE payload (rendering neighborhood-demographics entries with `neighborhood_name`) — lives on the frontend repo.
