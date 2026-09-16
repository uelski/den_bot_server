# Denver Open Data RAG — Project Context

## Session start
Before beginning work, read `NEXT_STEPS.md` in the project root. It holds the current priority list (LangSmith, testing, memory, deployment, new data sources) with context on why each matters and what's already decided. When the user's request maps to one of those items, the file is the authoritative source for scope and sequencing.

## What This Is
Agentic RAG system over Denver City Open Data Catalog (ArcGIS FeatureServer services).
Users query in natural language; the system retrieves relevant GIS services and optionally
scrapes child layers dynamically.

## Stack
- **LangGraph**: Orchestrator agent graph with conditional routing
- **LangChain**: Retrieval, embeddings, LLM calls
- **Qdrant**: Vector DB (local Docker → Qdrant Cloud, managed)
- **FastAPI**: Streaming API layer
- **Embeddings**: Google gemini-embedding-001 (dense, 3072-dim) + BM25 (sparse) = hybrid retrieval
- **LLM**: Google gemini-2.5-flash — the default in every node; override with `GEMINI_MODEL`

## Data
- Source: `data/enriched_denver_catalog_cleaned.json`
- Each record: service_name, base_url, description, layers[{id, name, fields}], semantic_summary
- Embedded field: `semantic_summary`
- Key metadata for agent routing: `base_url`, `has_layers`, `full_metadata` (full JSON)

## Agent Architecture
### Orchestrator (app/graph/orchestrator.py)
State → Retrieve → Grade → [Generate | Scrape → Generate]

### Nodes
- **retrieve**: Hybrid vector search on Qdrant
- **grade**: LLM decides if retrieved docs are relevant
- **generate**: Stream final response
- **scraper**: Conditionally called when `has_layers=True` and user needs field-level detail;
  dynamically builds URL: `{base_url}/{layer_id}/query?...` and scrapes child layer data

### Routing Logic
- If graded docs are relevant → generate
- If graded docs are relevant AND query needs field detail → scrape → generate
- If no relevant docs → rewrite query → retrieve (max 2 retries)

## Agent Tools
`app/tools/registry.py` is the single registry and the cleanest seam for driving
this system programmatically — the tools are plain functions with `@tool`
decorators, importable without standing up FastAPI or the graph.

Adding one is: write the function, `@tool`-decorate it, append to `AGENT_TOOLS`.
No graph or router changes.

| Tool | Backing service |
|---|---|
| `get_neighborhood_weather` | NWS |
| `get_rtd_service_alerts` | RTD GTFS-realtime |
| `get_rtd_next_arrivals` | RTD GTFS-realtime |
| `get_rtd_vehicle_positions` | RTD GTFS-realtime |
| `search_denver_gov` | Tavily |

## API
Full surface (`app/main.py` + three routers). Relevant if you're driving this
service from the outside rather than importing it.

- POST /query — body: {query: str, thread_id?: str}, response: streaming text/event-stream
- GET /health
- POST /feedback — Resend-backed; 503s without RESEND_API_KEY
- GET /knowledge-base/documents — list ingested KB documents
- GET /knowledge-base/documents/download — signed download URL (file-backed docs only)
- POST /admin/validate-password — lets the frontend gate its admin UI
- POST /admin/pdf-upload-url — short-TTL signed upload URL, metadata baked into the signature
- GET /ping — keepalive that touches Qdrant + Redis so the free-tier managed resources aren't reaped for inactivity; hit on a schedule by GCP Cloud Scheduler (see app/keepalive.py)

## Environment Variables (.env)
- GEMINI_API_KEY
- QDRANT_URL (default: http://localhost:6333)
- QDRANT_COLLECTION_NAME (default: denver_gis_catalog)
- QDRANT_KB_COLLECTION_NAME (default: denver_pdf_knowledge_base) — PDF knowledge base; searched alongside the catalog by the retriever
- COHERE_API_KEY — required by the reranker node (rerank-english-v3.0) that merges catalog + PDF KB hits; fails open to fused order if unset
- REDIS_URL (default: redis://localhost:6379) — backs the LangGraph checkpointer for multi-turn memory; unset to run single-turn
- TAVILY_API_KEY — required for the search_denver_gov agent tool
- RESEND_API_KEY — required for POST /feedback to deliver mail (without it the endpoint 503s)
- FEEDBACK_TO_EMAIL — destination address for feedback emails (your inbox)
- FEEDBACK_FROM_EMAIL — sender address. Default `onboarding@resend.dev` works ONLY for delivery to the email registered on the Resend account. Override once a sending domain is verified.

## Writing a new ingest script
`docs/ingest-field-shape.md` is the authoritative metadata + URL shape — POI vs
aggregate-per-neighborhood, the shared-`doc_type` discriminator pattern, and the
`--purge` scoping trap. Read it before adding a data source.

## Dev Notes
- Local infra: `docker compose up -d` (brings up Qdrant + Redis with persistent named volumes; see `docker-compose.yml`)
- Ingest: `python scripts/ingest.py`
- Run API: `uvicorn app.main:app --reload`
- force_recreate=True in ingest.py is intentional for dev; set False for prod

## Databases
### Qdrant (vector search)
- Collection: denver_gis_catalog
- Hybrid search: dense (Google gemini-embedding-001) + sparse (BM25)
- Key metadata fields per point:
  - service_name, base_url, has_layers, hub_url, service_item_id, full_metadata
  - doc_type (str | None) — e.g. "neighborhood_demographics" tags the per-neighborhood ACS summary chunks
  - neighborhood_name, neighborhood_id, district_num, topic — set on neighborhood_demographics docs

Demographics are ingested as per-neighborhood, per-topic NL summary chunks
(see `scripts/generate_neighborhood_summaries.py` and `scripts/ingest_neighborhoods.py`).

## Agent Routing Logic
After retrieval and grading, route based on metadata:
1. has_layers=True → scrape ArcGIS live → generate
2. default → generate with hub_url or base_url as reference link