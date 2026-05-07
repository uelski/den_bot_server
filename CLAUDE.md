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
- **Qdrant**: Vector DB (local Docker → GKE)
- **FastAPI**: Streaming API layer
- **Embeddings**: Google text-embedding-004 (dense) + BM25 (sparse) = hybrid retrieval
- **LLM**: Google gemini-3.1-flash-lite-preview 

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

## API
- POST /query — body: {query: str}, response: streaming text/event-stream
- GET /health

## Environment Variables (.env)
- GEMINI_API_KEY
- QDRANT_URL (default: http://localhost:6333)
- QDRANT_COLLECTION_NAME (default: denver_gis_catalog)
- REDIS_URL (default: redis://localhost:6379) — backs the LangGraph checkpointer for multi-turn memory; unset to run single-turn
- TAVILY_API_KEY — required for the search_denver_gov agent tool
- RESEND_API_KEY — required for POST /feedback to deliver mail (without it the endpoint 503s)
- FEEDBACK_TO_EMAIL — destination address for feedback emails (your inbox)
- FEEDBACK_FROM_EMAIL — sender address. Default `onboarding@resend.dev` works ONLY for delivery to the email registered on the Resend account. Override once a sending domain is verified.

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