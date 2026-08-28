# Next steps

Living priority list for the Denver Open Data RAG project. Ordered by what
delivers the most leverage for the least effort first. Scope of each item is
intentionally loose — refine when picking something up.

**Companion doc:** `ITERATION_V2.md` holds design rationale and decisions
(why the PDF KB is shaped the way it is, why Reddit was deferred). This file
holds *status* — what's done, what's in flight, what's next. When they
disagree, trust this one for status and that one for reasoning.

---

# YOU ARE HERE (last updated 2026-08-27)

**Last commit on this branch: 2026-06-23.** If you're reading this cold after
a gap, this section is the whole handoff. Everything below it is background.

### State of the world

- **Branch:** `feature/features_v2`, unpushed commits only.
- **Prod is healthy and current.** Everything through PR #43 is merged to
  `main` and deployed: PDF knowledge base end-to-end, catalog+KB retrieval
  fan-out with Cohere rerank, public KB read API, `/ping` keepalive.
- **Unpushed on `feature/features_v2`:** the page-ingest script (`67fca0f`),
  the keep-set trim, the two citation branches + tests, and doc updates.
  `aab0be1` (deployment.md keepalive docs) rides along.
- **Test suite:** 754 passing. Run `python -m pytest -q` before any push.

### The one thing in flight

**denvergov.org HTML page ingest** — see the full section below. All code is
written and green. It has **never been run with `--write`**, so nothing has
entered Qdrant. Prod is completely unaffected by this branch.

### Exact next action, in order

1. ~~Decide the keep-set~~ ✅ **done 2026-08-27** — dropped Transparent Denver;
   5 pages remain.
2. ~~Two `doc_type` citation branches + tests~~ ✅ **done 2026-08-27** — see
   § "Citation branches" below.
3. **→ YOU ARE HERE. Run** `python scripts/ingest_denvergov_pages.py --write`
   (data-mutating; hand-run, not agent-run). Needs `QDRANT_URL` /
   `QDRANT_API_KEY` / `GEMINI_API_KEY` in env. Consider
   `--only 1` first to sanity-check a single page end-to-end.
4. **Verify** a page surfaces via `/query` — the answer should cite the page
   title with **no page number**, and `sources` should carry `source_url` +
   `doc_type: "denvergov_page"` with **no `document_id`** (so the frontend
   renders a link, not a Download button).
5. **Push, PR, merge** to `main`.

Step 3 is the first irreversible action. If a page ingests badly, re-running
after a fix overwrites it in place — `document_id` is the URL, so there's no
duplicate-chunk cleanup to do.

---

## 1. In flight — denvergov.org page ingest

Adding curated denvergov.org informational pages to the knowledge base — the
static city pages the Tavily `search_denver_gov` tool doesn't reliably surface.
Chosen over the Reddit alternative; that comparison is documented in
`ITERATION_V2.md` § "Knowledge base source expansion".

### What this is architecturally

A **second, thinner ingestion path into the same Qdrant KB collection** the
PDFs use. It deliberately bypasses the admin-upload pipeline (frontend →
signed URL → GCS → Pub/Sub → worker), because there is no file: pages are a
curated URL list, so there's no GCS object and therefore no `object.finalize`
event to trigger Pub/Sub. The script imports `worker/pipeline` directly and
writes to Qdrant from the CLI.

**Shared with the PDF path:** the KB collection, the chunk/embed/upsert code,
the payload shape, the `category` allowlist.

**Different from the PDF path:**
- Trigger is a CLI run (and could be scheduled), not a human upload event.
- `document_id` is the page **URL** — *stable*, so a re-fetch overwrites in
  place. PDFs use a timestamped `document_id`, which is exactly why
  re-uploading a corrected PDF duplicates instead of replacing it.
- `doc_type="denvergov_page"` distinguishes them downstream.

**No graph changes needed** — the retriever already fans out to the KB
collection and the reranker already merges mixed provenance.

### Done (commit `67fca0f`, 2026-06-23, unpushed)

- `scripts/ingest_denvergov_pages.py` — fetch (httpx) → extract main content
  (trafilatura) → reuse `worker/pipeline` chunk/embed/upsert.
  **Dry-run by default (no Qdrant writes).** `--write` to ingest,
  `--only IDX` for a subset.
- `trafilatura` added to `requirements.txt` (scripts-only dependency).
- **Dry-run ran clean** — all 6 pages extracted, no JS-rendering problem
  (the concern didn't materialize).

| # | Page | Category | Extraction quality | Kept? |
|---|---|---|---|---|
| 1 | City Council Backgrounder | `council` | substantive | ✅ |
| — | Transparent Denver | `transparency` | thin — ~549 chars, mostly links | ❌ dropped |
| 2 | City Budget | `budget` | substantive | ✅ |
| 3 | Financial Reports | `finance` | substantive | ✅ |
| 4 | Investments & Debt | `finance` | thin — ~895 chars | ✅ |
| 5 | Search for Records | `general` | substantive | ✅ |

### Keep-set — decided 2026-08-27

**Transparent Denver dropped**; the other five kept. It extracted to ~549
chars of almost entirely navigation links, and a chunk that thin can only
dilute retrieval. Its child pages carry the real content — Investments & Debt
is already in the list; add more if they prove substantive. Investments & Debt
is thin too but carries genuine content, so it stays.

### Citation branches — done 2026-08-27

Both downstream paths used to assume "KB document = uploaded PDF". They now
branch on `doc_type` via a shared helper, so **any** future non-PDF KB source
(Reddit, other scrapes) gets correct citation behavior for free rather than
needing another special case.

- **`app/retrieval/kb.py`** — new `is_file_backed_kb_doc(metadata)` +
  `FILE_BACKED_KB_DOC_TYPES`. Uploaded PDFs predate the `doc_type` field and
  don't set it, so an absent/empty `doc_type` means PDF. This is the single
  place to register a future file-backed type.
- **`app/main.py`** (`build_sources_payload`) — file-backed docs emit
  `document_id` + page range as before. Non-file docs emit `doc_type` and
  `source_url` and **omit `document_id`**, so the frontend renders a link
  rather than a Download button that would 400.
- **`app/graph/nodes/generator.py`** (`_format_docs`) — file-backed docs cite
  `[Title, pages N–M]`; non-file docs cite `[Title]` alone, since a scraped
  page always chunks to "page 1" and printing it would claim precision that
  doesn't exist.
- **Tests** — 5 added (2 in `test_generator_format.py`, 3 in
  `test_main_sse.py`), plus a `denvergov_page_doc_factory` fixture in
  `tests/conftest.py`. Suite at 754.

**Frontend note:** the `sources` SSE entry for a page has no `document_id`.
If the frontend keys its Download button off that field's presence it needs no
change; if it assumes every `knowledge_base` entry is downloadable, it needs a
guard.

### Deferred out of this batch

- **Checkbook page** — JS SPA, needs headless rendering or a data API.
- **Municode ordinances** — huge separate corpus, different ingestion shape.
- **Sitemap crawl** — v1 is a hardcoded curated list; crawling is a later call.
- **Scheduled re-fetch** — stable `document_id` makes it idempotent and
  therefore easy, but nothing schedules it yet.

---

## 2. Next up (nothing started)

### Eval harness
The natural follow-up to the test suite. ~20 representative queries with
expected behavior in a LangSmith dataset, to regression-test prompt and model
changes. The rerank-score run metadata (commit `fe8196d`) is the ready signal
for this. Not blocking anything; pick it up when prompt-tuning starts to feel
risky.

### Geo-filtered retrieval
Unblocked by data availability — almost every doc type now carries
`metadata.location = {lat, lon}` (demographics, parks, libraries, rec centers,
schools, RTD stops, plus copied centroids on per-neighborhood crime/traffic
docs), and Qdrant supports `geo.radius` filters natively.

- Add a geo-filter pass in `app/graph/nodes/retriever.py` when the
  intent_router detects a "near X neighborhood" pattern.
- Resolve neighborhood → centroid via existing demographics docs or the
  resolver + geojson directly.
- Could also become a `find_nearby` tool taking neighborhood + doc_type.

**Effort:** ~half a day for the retriever-side filter; another half if wrapped
as a tool.

### More structured data sources
Patterns are well-established (POI vs aggregate-per-neighborhood — see
`ingest_field_shape_convention.md` in memory); each is roughly half a day.

- **Denver Assessor property data** — "what are properties worth in my
  neighborhood", pairs naturally with demographics. High user value.
- **Building permits** — POI shape; "what's being built in RiNo lately".
- **311 call data** — aggregate-per-neighborhood; "common complaints in Five
  Points".
- **Business / restaurant licenses** — neighborhood character beyond demographics.
- **Eviction filings** — Denver Courts publish these; pairs with housing demographics.
- **ACS vintages beyond 2017–2021** — comparable demographics over time.
- **OSM / additional POI** — probably enriches existing summaries rather than
  shipping as a new doc type.

### Reddit as a knowledge source — deferred, gated
Not an effort problem; the ingestion side is nearly identical to page ingest.
Blocked on an unresolved **API/ToS access-path decision** (post-2023 Reddit
API is paid and rate-limited; scraping violates ToS), plus a real
provenance-design problem — anecdote must never blend into factual civic
claims. Full reasoning in `ITERATION_V2.md` § "Knowledge base source
expansion".

### Pre-share polish (from ITERATION_V2.md)
Worth revisiting before sharing publicly: p50/p95 latency measurement, mobile
UI testing, graceful external-API fallbacks, and "what can I ask?" onboarding
examples. See `ITERATION_V2.md` § "Fix Before Adding".

---

## 3. Known cleanups (small, non-blocking)

- `scripts/ingest.py` should `create_payload_index('metadata.neighborhood_name',
  'keyword')` after collection creation — a future re-ingest would otherwise
  drop the index Qdrant Cloud strict mode requires.
- `CLAUDE.md` says the embedding model is `text-embedding-004` (768d). Actual
  is `gemini-embedding-001` (3072d).
- `scripts/viewer_upsert.py` hardcodes `http://localhost:6333` with no
  `api_key`. Local-only today; needs the standard env-var pattern if ever
  pointed at prod.
- **PDF KB has no delete/replace utility.** Because `document_id` is
  timestamped, re-uploading a corrected PDF *duplicates* rather than replaces —
  removal is a manual Qdrant filter-delete today. (Page ingest doesn't have
  this problem; its `document_id` is stable.)
- **Hub URL audit** — `scripts/audit_hub_urls.py` + `apply_hub_url_updates.py`.
  Run periodically (quarterly?) or when broken links are reported.

---

## Completed

### PDF Knowledge Base — shipped, live in prod (2026-06)
The largest epic to date. Design rationale is in `ITERATION_V2.md`; what
actually shipped:

- **Admin auth** — `app/admin.py`, `APIRouter(prefix="/admin")`.
  Password-per-request with `hmac.compare_digest`, rate-limited,
  `POST /admin/validate-password` so the frontend can gate its admin UI.
- **Signed-URL upload flow** — `POST /admin/pdf-upload-url` returns a
  short-TTL signed URL with upload metadata baked into the signature
  (tamper-proof: a mismatched PUT header 403s). PDF bytes never touch FastAPI.
- **Worker service** — `worker/`, a separate Cloud Run service.
  `POST /pubsub/pdf-ingest` receives the GCS `object.finalize` Pub/Sub push →
  download → pymupdf parse → parent/child chunk → embed → upsert. Deployed via
  `cloudbuild.worker.yaml`.
- **Retrieval integration** — `app/graph/nodes/retriever.py` fans out to
  catalog + KB concurrently via `asyncio.gather` (20 candidates each);
  `app/graph/nodes/reranker.py` merges with Cohere `rerank-english-v3.0`,
  drops low-relevance docs by score threshold, and attaches rerank scores as
  LangSmith run metadata.
- **Public KB read API** — list documents + download URL; `sources` SSE events
  carry `document_id` for in-chat download.

### Multi-turn memory — shipped
Redis-backed LangGraph checkpointer (`app/graph/memory.py`). `thread_id`
accepted per request; graph compiled with the checkpointer at FastAPI lifespan
startup, with a stateless fallback when `REDIS_URL` is unset.
`main_router` sees conversation history, so follow-ups like "how about Park
Hill?" classify against the prior topic instead of the bare query.

**Redis note:** Redis Cloud free tier on Redis 8, which bundles RediSearch +
RedisJSON into core. Memorystore and Upstash were both attempted and failed —
Memorystore ships zero modules; Upstash's RediSearch omits `FT._LIST`, which
`AsyncRedisSaver.asetup()` requires. Don't re-litigate this.

### Deployment — shipped, live
GCP project `blue-cypher`, region `us-east4`. Cloud Run + Artifact Registry +
Cloud Build GitHub trigger + Secret Manager, all declarative via
`cloudbuild.yaml`. Qdrant Cloud migrated by snapshot/restore with a verified
byte-identical parity test. GTFS lookup tables baked into the image
(`cd69517`). `GET /ping` keepalive touches Qdrant + Redis on a Cloud Scheduler
cadence so free-tier managed resources aren't reaped for inactivity. Full
runbook in `deployment.md`.

**Remaining deployment nice-to-haves:** rate limiting on `/query` (Gemini calls
aren't free), and a lightweight auth layer if the frontend ever stops being our
own trusted deployment.

### Agent tools — 5 shipped
`app/tools/registry.py` is the single registry. Adding one is: write the
function, `@tool`-decorate it, append to `AGENT_TOOLS` — no graph or router
changes.

`get_neighborhood_weather` (NWS) · `get_rtd_service_alerts` ·
`get_rtd_next_arrivals` · `get_rtd_vehicle_positions` ·
`search_denver_gov` (Tavily)

### Data sources — 9 ingests shipped
All follow `ingest_field_shape_convention.md` (in memory) for URL/metadata shape.

| Source | Pattern | Docs |
|---|---|---|
| GIS service catalog | base catalog | — |
| Neighborhood demographics (ACS 2017–2021) | aggregate per neighborhood | 78 |
| Parks | POI | 374 |
| Crime | aggregate per neighborhood | 78 |
| Libraries | POI | 27 |
| Rec centers | POI | 31 |
| Public schools | POI (`institution_type` discriminator) | 232 |
| Non-public schools | POI (`institution_type` discriminator) | 46 |
| Traffic accidents | aggregate per neighborhood | ~78 |
| RTD GTFS (stops/routes) | POI | — |

### URL field shape refactor — shipped (`4ee6dd6`)
Split the dataset citation URL from the per-entity map URL.
`build_map_viewer_links` prefers `metadata.map_url` over `hub_url` and
`display_name` over `service_name`; both backward-compatible. Authoritative
reference: `ingest_field_shape_convention.md` in memory.

### LangSmith observability — shipped
`.env` carries `LANGCHAIN_API_KEY`, `LANGCHAIN_TRACING_V2=true`,
`LANGCHAIN_PROJECT=blue-cypher-dev`. Every `/query` run is tagged
`run_name: "/query: <preview>"` + `api-query` so traces are searchable.

### Testing — shipped
`pytest` + `pytest-asyncio` via `pytest.ini`. **749 tests.** Shared fixtures in
`tests/conftest.py` (`make_doc`, `catalog_doc`, `neighborhood_doc_factory`,
`mock_llm`). Covers SSE payload helpers, node-level behavior, conditional-edge
routing, graph structure, and external tools.

---

## Deferred / considered and skipped

- **Resolver wired into retriever** — canonicalize neighborhood names before
  retrieval ("RiNo poverty" → "Five Points poverty"). Small, still worth doing.
- **LLM-grounded route name resolver** — analogue to the neighborhood resolver
  for "W Line", "Route 15". Tier 3 ships with a lexical fallback in
  `rtd_vehicles.resolve_route_id`; upgrade when traces show it failing.
- **Semantic query cache** — Redis-backed, to skip redundant LLM calls on
  similar queries.
- **Neighborhood alias dictionary** — partly covered by the resolver's ALIASES tier.
- **`bluecypher` CLI** — wrap ingest + query for reuse on other cities' data.
- **User document upload / per-user local vector store** — personal context
  layer (lease, insurance policy) cross-referenced with civic data.
