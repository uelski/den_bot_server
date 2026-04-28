# Next steps

Living priority list for the Denver Open Data RAG project. Ordered by what delivers the most leverage for the least effort first. Scope of each item is intentionally loose — refine when picking something up.

## Completed

### LangSmith observability — done
Tracing is live. `.env` carries `LANGCHAIN_API_KEY`, `LANGCHAIN_TRACING_V2=true`, and `LANGCHAIN_PROJECT=blue-cypher-dev`. `app/main.py` tags every `/query` run with `run_name: "/query: <preview>"` and the `api-query` tag so traces are searchable. `.env.example` documents the contract for new contributors.

### Testing — done (initial pyramid)
`pytest` + `pytest-asyncio` in place via `pytest.ini`. 93 passing tests across:
- SSE payload helpers (`build_sources_payload`, `build_map_viewer_links`, `_summarize_tool_output`).
- Node-level: retriever, intent_router, generator `_format_docs`, tool_agent, neighborhood resolver.
- Routing: `route_after_*` conditional edges + graph structure smoke test (regression guard for removed `pg_query` node).
- External tools: weather (cached + uncached paths, NWS two-hop, period parsing).
- Refactored `app/main.py` to extract pure helper functions out of the SSE event_stream closure for testability.

**Eval harness** is the natural follow-up — once we have ~20 representative queries with expected behavior, LangSmith datasets can regression-test prompt/model changes. Not blocking anything; pick up when prompt-tuning starts to feel risky.

---

## 1. Memory (multi-turn conversations)

Today the graph is stateless per request — each `/query` starts from scratch. The frontend can't ask follow-up questions like "what about Five Points?" after a query about Capitol Hill.

**Tasks**:
- Add a LangGraph `Checkpointer` (start with `MemorySaver` for local dev, upgrade to `PostgresSaver` or `RedisSaver` later for prod).
- Accept a `thread_id` in the `/query` request body; pass it in `config={"configurable": {"thread_id": ...}}` when invoking the graph.
- The `messages` state field (already `Annotated[list, add_messages]`) will accumulate across turns automatically via the reducer.
- Update the generator prompt to treat prior messages as context, not just the latest query.
- Frontend: generate/manage a thread_id per chat session and send it with each query.

**Decision**: whether to persist memory across API restarts (`PostgresSaver`) or keep it in-process only (`MemorySaver`). In-process is fine for now; upgrade when we deploy.

**Effort**: ~half a day for in-process memory end-to-end including frontend changes.

## 2. Deployment

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

## 3. More data sources

The catalog today is Denver GIS services from ArcGIS FeatureServer plus per-neighborhood demographic chunks. Plenty of other high-signal Denver open data to pull in.

**Top candidates from the ArcGIS catalog** (already partially indexed via `scripts/ingest.py`, but their child layers / individual records aren't yet retrievable):
- **Crime data** (ArcGIS catalog) — Denver Police publishes incident-level data; high agent utility for "is X neighborhood safe" type questions. Needs thoughtful privacy framing and probably a temporal filter (recent vs historical).
- **Parks** (ArcGIS catalog) — POI-style data: park name, amenities, location. Pairs well with neighborhood lookups ("parks near Capitol Hill") and the lat/lon centroids we already have.
- **Building permits** (ArcGIS catalog) — city-wide development signal; "what's being built in RiNo lately" queries.

**Other candidates** (rougher prioritization by agent utility):
- **311 call data** — "what are common complaints in Five Points" type queries. CSV/JSON from opendata-geospatialdenver; not a FeatureServer.
- **ACS vintages beyond 2017-2021** — let users compare a neighborhood's demographics over time.
- **OSM / POI data** — schools, transit stops; could enrich neighborhood summaries.
- **Non-ArcGIS Denver sources** — open data portal CSVs without FeatureServer backing; separate ingestion path.

**Tasks** (per source):
- Write a small enrichment script that transforms the raw source into the same Document shape used for either the catalog or neighborhood-demographics pattern.
- Decide on chunking strategy (whole-record, per-section, per-incident, etc.).
- Tag with a distinct `doc_type` so retrieval filters and SSE rendering can branch appropriately (mirror the `neighborhood_demographics` pattern).
- Add any source-specific `hub_url` logic.

**Decision**: each source roughly doubles the ingestion surface area. Pick one of crime / parks / building permits to ship first and generalize patterns from there. Parks is probably the lowest-effort starter (smallest dataset, cleanest schema, well-defined POI shape) before tackling the higher-value but messier crime/permits data.

**Effort**: ~1 day per source for initial integration, less for subsequent ones as patterns solidify.

---

## Not on the list but worth noting

- **Hub URL audit** has its own workflow in `scripts/audit_hub_urls.py` + `scripts/apply_hub_url_updates.py` — run periodically (quarterly?) or when broken links are reported. See `hub_url_audit_system.md` in memory.
- **Frontend adaptation** to the enriched `sources` SSE payload (rendering neighborhood-demographics entries with `neighborhood_name`) and the new `tool_call` / `tool_result` events — lives on the frontend repo.
- **NWS weather tool** is shipped on `feature/geodata` (commit `44fd48b`): tool-calling branch with a 3-way main_router, `app/graph/nodes/tool_agent.py` ReAct loop, weather as the first registered tool, SSE `tool_call` / `tool_result` events. Adding more tools = function + entry in `AGENT_TOOLS`.
