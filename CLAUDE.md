# Denver Open Data RAG — Project Context

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

## Dev Notes
- Run Qdrant locally: `docker run -p 6333:6333 qdrant/qdrant`
- Ingest: `python scripts/ingest.py`
- Run API: `uvicorn app.main:app --reload`
- force_recreate=True in ingest.py is intentional for dev; set False for prod

## Databases
### Qdrant (vector search)
- Collection: denver_gis_catalog
- Hybrid search: dense (Google text-embedding-004) + sparse (BM25)
- Key metadata fields per point:
  - service_name, base_url, has_layers, hub_url, service_item_id, full_metadata
  - pg_table (str | None) — if set, agent should query Postgres instead of ArcGIS live
  - topic (str | None) — e.g. "demographics"
  - is_current (bool | None) — marks authoritative/current datasets

### Postgres
- DB: blue_cypher_data
- Table: neighborhood_demographics
- 178 rows, one per Denver neighborhood
- Key columns: NBHD_NAME (text, indexed), NBHD_ID, DIST_NUM, Nmbr_Population,
  Nmbr_PerCapitaIncome, Pct_PopulationInPoverty, Pct_PopulationMinority etc.
- Connection: POSTGRES_URL in .env

## Agent Routing Logic
After retrieval and grading, route based on metadata:
1. pg_table is set → query Postgres with LLM-generated WHERE clause → generate
2. has_layers=True, no pg_table → scrape ArcGIS live → generate
3. default → generate with hub_url or base_url as reference link