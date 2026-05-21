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

### Qdrant Cloud — done (2026-05-19)
Migrated via snapshot/restore (bit-for-bit) into `us-east4` cluster. Phase 5 parity test confirmed payloads byte-identical and top-10 retrieval overlap 100% across 15 representative queries. App-side `.env` flipped to the managed URL + API key; local Docker Qdrant kept running through ~2026-05-26 as a fallback.
- One payload index added on `metadata.neighborhood_name` (keyword) on both clusters — required by Qdrant Cloud's `strict_mode_config.unindexed_filtering_retrieve=false`. Filters live in `app/tools/weather.py` and `app/tools/rtd_arrivals.py`.
- Snapshot file lives under `snapshots/` (gitignored); safe to delete after the fallback window closes.

### Cloud Build + Cloud Run scaffolding — done (2026-05-19, in commits on `feature/server-deploy`)
GCP project `blue-cypher`, region `us-east4`. Artifact Registry repo + Cloud Build GitHub trigger + Cloud Run service + 9 Secret Manager secrets all provisioned via the console. `cloudbuild.yaml` builds → pushes → deploys with `--set-secrets` (declarative pattern: YAML is source of truth). See `deployment.md` for the full console runbook.

### Paused mid-Phase F — resume sequence

First deploy crashed on `redis.exceptions.ResponseError: JSON module is not loaded`. Memorystore Basic doesn't ship RedisJSON/RediSearch modules that `langgraph-checkpoint-redis` requires. Pivoted to **Upstash Redis** (free tier, modules included, public TLS — no VPC). Commit `9345764` drops `--vpc-connector` from `cloudbuild.yaml`.

To resume (full detail in memory `next_focus_deployment.md`):
1. Provision Upstash DB (`den-bot-redis`, Regional, us-east-1, TLS)
2. Update `REDIS_URL` secret with new `rediss://default:<pw>@<id>.upstash.io:6379` value
3. Push `feature/server-deploy` and merge to main → Cloud Build trigger fires
4. Smoke from frontend (queries in `deployment.md` Phase F.3)
5. Tear down Memorystore + VPC connector (~$45/mo) once new deploy is green

### Remaining (after Cloud Run is green)
- Tighten `ALLOWED_ORIGINS` for prod (already done — only `bluecypher.ai` + `www.bluecypher.ai` in `cloudbuild.yaml`).
- Add a lightweight auth layer if the frontend isn't going to be our own trusted deployment.
- Rate limiting (token bucket on `/query`) — Gemini calls aren't free.

### Follow-ups surfaced during Qdrant migration (non-blocking)
- `scripts/ingest.py` should `create_payload_index('metadata.neighborhood_name', 'keyword')` after collection creation, so a future re-ingest doesn't drop the index that strict mode requires.
- CLAUDE.md says embedding model is `text-embedding-004` (768d). Actual is `gemini-embedding-001` (3072d) — update the doc.
- `scripts/viewer_upsert.py` hardcodes `http://localhost:6333` with no `api_key`. Local-only utility today; needs the standard env-var pattern if ever pointed at prod.

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
