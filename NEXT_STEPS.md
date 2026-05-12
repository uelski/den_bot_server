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

### NWS weather tool — done (first agent tool, on `feature/geodata`)
- 3-way `main_router` (general / data_search / tool); orchestrator routes `needs_tool` to a new `app/graph/nodes/tool_agent.py` ReAct loop.
- `app/tools/registry.py` is the single registry of `@tool`-decorated functions bound to Gemini. First entry: `get_neighborhood_weather`, wrapping `app/tools/weather.py` (resolver → Qdrant lat/lon → NWS API).
- Generator branches on `state.tool_results` to use a tool-aware prompt variant; streaming SSE token events flow as usual.
- New SSE events: `tool_call` and `tool_result` (forwarded from LangChain's `on_tool_start` / `on_tool_end`, filtered to `langgraph_node == "tool_agent"`).
- NWS reliability: 30-min TTL cache and 4-decimal coordinate rounding to avoid 301 redirects.
- Adding the next tool is now: write the function, decorate with `@tool`, append to `AGENT_TOOLS`. No graph or router changes required.

### URL field shape refactor — done (commit `4ee6dd6`)
SSE contract update that split the dataset citation URL from the per-entity map URL on parks + RTD docs (and now seeds the convention for every later ingest). `app/main.py:build_map_viewer_links` prefers `metadata.map_url` over `hub_url` for the URL and `display_name` over `service_name` for the label — both backward-compatible. See `ingest_field_shape_convention.md` in memory for the authoritative reference.

### More data sources — substantially done
Six new data sources shipped using the patterns above. `ingest_field_shape_convention.md` in memory codifies the shape so future sources are mostly templated work.

| Source | Pattern | Doc count | Branch / commit |
|---|---|---|---|
| Parks | POI (one doc per park) | 374 | `feature/parks-data` (PR #17) |
| Crime | Aggregate per neighborhood | 78 | `feature/crime-data` (PR #18) |
| Libraries | POI | 27 | `feature/new-city-data` `5a1da5c` |
| Rec centers | POI | 31 | `feature/new-city-data` `15f5b3f` |
| Non-public schools | POI (with `institution_type` discriminator) | 46 | `feature/new-city-data` `e443167` |
| Public schools | POI (with `institution_type` discriminator) | 232 | `feature/new-city-data` `b656dec` |
| Traffic accidents | Aggregate per neighborhood | ~78 | `feature/new-city-data` `0568075` |

Remaining "More data sources" candidates from earlier — patterns now well-established, each is roughly half a day:
- **Building permits** — POI shape; "what's being built in RiNo lately" type queries.
- **311 call data** — likely aggregate-per-neighborhood; "what are common complaints in Five Points".
- **ACS vintages beyond 2017–2021** — comparable demographics over time.
- **OSM / additional POI data** — could enrich existing summaries rather than ship as new doc types.

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

## 2. Deployment — active focus

`deployment.md` already sketches the GCP Cloud Run + Qdrant Cloud path. Picking this up after the frontend deploy at bluecypher.ai. CORS already updated in `.env` / `.env.example` to allow the apex + www origins.

**Tasks**:
- **Managed Qdrant (Qdrant Cloud)** — decision locked, not GKE self-host. Provision the cluster, then migrate data from local Docker Qdrant. Migration path: re-run the ingest scripts against the prod URL/API key — `scripts/ingest.py`, `scripts/ingest_neighborhoods.py`, and every POI / aggregate ingest under `scripts/ingest_denver_*` and `scripts/ingest_rtd_gtfs.py`. Alternative if any local state isn't reproducible from scripts: `qdrant-client` snapshot/restore.
- **Redis Memorystore on GCP** — backs the LangGraph checkpointer (`REDIS_URL`). Provision the instance, attach Cloud Run via a VPC connector. The checkpointer code doesn't care about backing store, just the connection string.
- **Cloud Run for the FastAPI server** — build + push container to Artifact Registry. Set as Cloud Run secrets: `GEMINI_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `REDIS_URL`, `RESEND_API_KEY`, `FEEDBACK_TO_EMAIL`, `FEEDBACK_FROM_EMAIL`, `LANGCHAIN_*`, `TAVILY_API_KEY`, `ALLOWED_ORIGINS`.
- Tighten `ALLOWED_ORIGINS` for prod (drop the localhost entries from the deployed value).
- Add a lightweight auth layer if the frontend isn't going to be our own trusted deployment.
- Rate limiting (token bucket on `/query`) — Gemini calls aren't free.

**Effort**: 1-2 days depending on GCP familiarity.

## 3. Geo-filtered retrieval (now unblocked)

Almost every doc type now carries `metadata.location = {lat, lon}` — demographics, parks, libraries, rec centers, schools, RTD stops, plus copied centroids on the per-neighborhood crime / traffic docs. Qdrant supports `geo.location` filters natively, so we can now filter "parks within 1km of resolved neighborhood centroid" before semantic search.

**Tasks**:
- Pick a representative query like "parks near Capitol Hill" and add a geo-filter pass in `app/graph/nodes/retriever.py` when the intent_router detects a "near X neighborhood" pattern.
- Resolve the neighborhood → centroid via the existing demographics docs (or the resolver+geojson directly).
- Apply Qdrant `geo.radius` filter to narrow the candidate set before hybrid search runs.
- Likely tool path too — could become a `find_nearby` tool that takes a neighborhood + doc_type filter and returns ranked points.

**Effort**: ~half a day for the initial retriever-side filter; another half-day if we wrap as a tool.

---

## Not on the list but worth noting

- **Hub URL audit** has its own workflow in `scripts/audit_hub_urls.py` + `scripts/apply_hub_url_updates.py` — run periodically (quarterly?) or when broken links are reported. See `hub_url_audit_system.md` in memory.
- **Frontend adaptation** to the enriched `sources` SSE payload (rendering neighborhood-demographics entries with `neighborhood_name`) and the `tool_call` / `tool_result` events — lives on the frontend repo. The post-refactor `map_url` / `display_name` fields are backward-compatible so nothing on the frontend should have broken when those landed.
- **Resolver wired into retriever** — discussed but deferred. Canonicalize neighborhood names before the retriever runs (so "RiNo poverty" → "Five Points poverty"). Small future change.
- **LLM-grounded route name resolver** — analogue to the neighborhood resolver for "W Line", "Route 15". Tier 3 ships with a lexical fallback in `rtd_vehicles.resolve_route_id`. Upgrade when traces show lexical failures.
