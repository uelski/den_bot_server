# Blue Cypher V2 — Pre-Share Checklist & Next Steps

## Fix Before Adding

Before sharing publicly, address these fundamentals first. They determine
whether people find the tool useful or bounce immediately.

### Response Quality
- Responses should be direct and data-driven, not indirect dataset references
- **Target**: "Five Points had 847 reported incidents last year, down from 923 the prior year — mostly property crime"
- **Avoid**: "Here is a link to the crime dataset that might help"
- Review every data source and confirm the generation node produces confident, specific answers
- add reranker

### Response Speed
- Measure p50 and p95 latency before sharing publicly
- Normal users will assume the tool is broken if full response exceeds 8-10 seconds
- Streaming helps perception but doesn't fix underlying latency issues
- Profile which nodes in the LangGraph graph are slowest and optimize first

### Mobile Experience
- Reddit and LinkedIn traffic is heavily mobile
- Test the full UI on a phone screen before sharing
- A broken mobile experience will lose most of that audience immediately

### Error Handling
- Define graceful fallbacks for: RTD down, NWS timeout, Qdrant returning no results
- Users need a clear "I couldn't find that" message, not a spinner or 500 error
- Every external API call should have a timeout and a fallback response

---

## High Value UI/UX Additions

### "What Can I Ask?" Onboarding
The single highest ROI UI change before sharing publicly. First-time users
have no idea what the system knows about.

- Add example queries on the landing page or as suggested prompts in the chat UI
- Examples:
  - "What are the demographics of RiNo?"
  - "Is there a rec center near Baker neighborhood?"
  - "What is the crime rate in Capitol Hill?"
  - "When is the next light rail from 16th & California?"
  - "What's the weather in Five Points this weekend?"

### Address-Based Queries
Most people think in terms of addresses, not neighborhood names.

- Add a geocoding step that converts a street address to a neighborhood + coordinates
- Options: Google Geocoding API or Census Bureau geocoder (free)
- Unlocks more natural queries like "what's near 2400 Curtis Street"
- High impact for usability with non-technical users

### Neighborhood Comparison Feature
The most shareable feature for Denver residents — especially those apartment hunting.

- Target query: "Compare Five Points and Baker for a young professional"
- Should touch demographics, crime, transit access, parks, and cost of living
- Return a structured comparison rather than a wall of text
- This is the kind of response people share with friends

### Conversational Follow-Ups
With Redis memory in place, ensure multi-turn actually works well end to end.

- "Tell me about crime in Capitol Hill" → "how does that compare to Five Points?" should work seamlessly
- Test a full multi-turn session covering neighborhood switch, topic switch, and follow-up questions
- This is what distinguishes Blue Cypher from a search engine

---

## Data Sources Worth Adding

### Denver Assessor Property Data ⭐
- "What is the assessed value of properties in my neighborhood?" is a common question
- Confirmed available on Denver Open Data portal
- Pairs naturally with existing neighborhood demographics data
- Relevant to homeowners, renters, and anyone exploring neighborhoods

### Denver Restaurant / Business Licenses
- Answers "what businesses are in my neighborhood?"
- Gives a sense of neighborhood character beyond demographics
- Useful for people moving to Denver or exploring new areas

### Eviction Filings
- Denver Courts publish eviction data publicly
- Paired with housing demographics answers meaningful questions about housing stability
- High value for journalists, advocates, and renters
- Context: Denver has had significant housing instability — this is a relevant topic

---

## PDF Knowledge Base (new collection + ingestion pipeline)

Add a second Qdrant collection for long-form legal/public PDFs from the City of
Denver — municipal code, council backgrounders, budget, financial reports.
Different doc shape, different retrieval pattern, different citation style;
keep it isolated from the existing catalog collection rather than overloading
metadata with a `doc_type` discriminator.

### Admin auth
Single admin (me), low traffic, hardcoded password in an env var.
**Password-on-every-request, no token / JWT / session.** A token's whole
value is "I issued you a session, here's proof" — with one user and one
shared secret, the password *is* the session. Tokens add the ceremony
without adding security.

**Implementation details that matter:**

- **Constant-time compare** on the server: `hmac.compare_digest(provided, expected)`, never `==`. Bad habit to skip even for a one-admin system.
- **`sessionStorage`, not `localStorage`** on the frontend. `localStorage` persists across browser sessions and is reachable from any XSS; `sessionStorage` clears on tab close.
- **HTTPS-only** — already true on Cloud Run, called out because passwords-in-headers over plain HTTP is the classic failure mode.
- **One `/admin/validate-password` endpoint** the frontend hits on login. Same password header, returns 200/401, stores nothing server-side. Lets the frontend avoid showing the admin UI to someone who typed the wrong password without inventing a session concept.
- **Rate-limit failed attempts** — a few per IP per minute. Cloud Run doesn't provide this; a simple in-memory counter is fine at one server replica.

**Upgrade path if more admins ever exist:** skip JWT-on-shared-password
(middle ground, low real-world value) and go straight to **Google OAuth +
email allowlist** (`ADMIN_EMAILS` env var). Leverages Google's auth, no
shared secret to protect, identity/revocation/audit logs come for free.
That's the actually-used-in-production pattern. Don't preemptively build.

### Ingestion pipeline
Admin-uploads-a-PDF flow with a real production queue. Volume will be tiny —
the queue is explicitly chosen as hands-on experience with production async
patterns, not because throughput demands it. Be honest about that trade-off
in commit messages so future-me doesn't second-guess the choice when looking
at the queue-depth metric.

**End-to-end flow:**

```
Frontend → POST /admin/pdf-upload-url (password header) → FastAPI
FastAPI → returns short-lived signed URL for a specific GCS object path
Frontend → PUT PDF bytes directly to GCS (bypasses API entirely)
GCS object.finalize → Pub/Sub notification (native, no manual publish)
Pub/Sub → push subscription → POST /pubsub/pdf-ingest on the worker service
Worker → download from GCS → parse → chunk → embed → upsert to Qdrant KB collection
```

**Why this shape:**

- **Signed-URL flow, not server-proxied upload.** PDF bytes never touch the FastAPI process — saves Cloud Run RAM/bandwidth and removes the "big upload times out the API" failure mode. Standard production pattern.
- **Password protection lives on the signed-URL endpoint**, not on the upload itself (because uploads no longer touch the API). Scope each issued URL tightly: short TTL (5–10 min), single use, restricted to one specific object path. Don't issue wildcard URLs. Log every issuance attempt (success + fail) — that's the auditable surface now.
- **Successful uploads are observed via the Pub/Sub message**, not via a server response. Failed uploads (user gets a URL, never completes the PUT) are invisible to us — acceptable trade for the single-admin case.
- **GCS-native Pub/Sub notification, not manual publish.** GCS emits to the topic on `object.finalize`. Removes the "we got the file but failed to enqueue" failure mode our original sketch had.
- **Separate Cloud Run service for the worker** (option B), not a new route on the API. Separate scaling, separate failure domain, separate logs; an API outage doesn't block ingestion and vice versa. This is the main thing that makes the exercise teach real production lessons.
- **Push subscription, not pull** — forces idiomatic handling of ack deadlines and 4xx-vs-5xx ack semantics (4xx ack-and-drop, 5xx nack-and-retry).
- **Dead-letter topic + max delivery attempts** from day one. With DLQ a poison PDF lands in a dead-letter bucket for inspection instead of wedging the queue.
- **Idempotent worker.** Pub/Sub is at-least-once and GCS can also redeliver notifications. Derive Qdrant point IDs deterministically from `(document_id, chunk_index)` — re-parsing the same PDF overwrites instead of duplicating. The GCS object name is a natural `document_id`.
- **Worker pipeline:** download PDF from GCS → parse (try `pypdf` first, escalate to `unstructured` or a vision model for scanned docs) → chunk → embed with the same Google `gemini-embedding-001` as the catalog (so cross-collection retrieval mixes cleanly in vector space) → upsert to the new collection.

### Upload metadata + categorization

Metadata is captured in the signed-URL request (call 1, JSON only) and bound
into the signed URL itself so the frontend can't tamper with it on the PUT
(call 2, file bytes only). No document registry / database in v1 — GCS
custom metadata + Qdrant payload covers everything until document-management
operations (list / delete / re-ingest) actually become painful.

**Two-call shape:**

```
1. POST /admin/pdf-upload-url     → FastAPI    (JSON metadata, no file)
   ← { signed_url, object_path, required_headers }

2. PUT  <signed_url>              → GCS direct (file bytes + required_headers verbatim)
```

The `required_headers` returned from call 1 are the `x-goog-meta-*` values
FastAPI baked into the signed URL. If the PUT in call 2 sends different
values (or omits any), GCS rejects with 403 — that's the tamper-proofing.
Frontend just spreads `required_headers` into the PUT call; no logic.

**Minimum metadata captured at upload time:**

| Field | Source | Purpose |
|---|---|---|
| `category` | frontend select, allowlist-validated server-side | retrieval filter + GCS path routing |
| `source_url` | frontend text input | citation back to denvergov.org |
| `document_title` | frontend text input | human-readable citation ("from \<title\>, page N") |
| `original_filename` | from file picker, editable | logs/debugging |
| `document_id` | server-derived (slugified object path) | idempotency key for Qdrant point IDs |
| `uploaded_at` | server-set when signed URL is issued | audit / sort order |

**Category, not collection.** Frontend selects a *category* (`ordinance` /
`council` / `budget` / `finance` / `transparency` / ...) which becomes a
Qdrant payload field. **Never let the frontend name the Qdrant collection
directly** — that's an injection vector into the catalog collection. One
PDF KB collection with a `category` filter scales better than N collections:
the retriever still fans out across just two (catalog + KB), and adding a
new category is a config change, not a graph change.

**Where each field ultimately lives:**
- **GCS object path** — encodes `category` and slugified filename for routing/inspection
- **GCS custom metadata (`x-goog-meta-*`)** — carries `source_url`, `document_title`, `document_id`, `uploaded_at` from upload through to the Pub/Sub notification
- **Qdrant point payload** — worker propagates everything above into every chunk, plus chunk-level fields (`page_number`, `chunk_index`, `source_collection: "knowledge_base"`)

**What we give up by skipping a registry:**
- "List all uploaded PDFs" — Qdrant filter query, dedupe by `document_id`
- "Delete a PDF and all chunks" — Qdrant filter-delete by `document_id`
- "Re-ingest" — re-upload via the same path; deterministic point IDs overwrite cleanly
- "Ingestion status / failures" — Cloud Run logs + DLQ contents only

Add a Firestore `documents` collection (or a small Postgres table) only if/when those operations start to bite. Don't preemptively build.

### Retrieval changes
Catalog and PDF KB get searched **in parallel** per query, then merged before
grading. A question about "Denver zoning" could legitimately want both the
catalog (the GIS layer) *and* the zoning PDF.

- **Retriever node** runs both collections via `asyncio.gather` — don't serialize or end-to-end latency doubles.
- **Reranker node becomes load-bearing**, not just a polish pass. It's the merge-and-reorder step on a mixed-provenance list. Cross-encoder rerank (Cohere Rerank, BGE-reranker, or similar) on top-20 → top-5 across both sources.
- **Per-doc provenance metadata** — every chunk carries `source_collection: "catalog" | "knowledge_base"` plus PDF-specific fields (document_title, page_number, section). Generator branches citation style: catalog hits show `hub_url`; PDF hits show "from <document title>, page N".
- **No `main_router` change needed** — `data_search` continues to mean "RAG path"; the retriever node decides what that means internally.

### Future doc types
PDFs first because they're the most common civic format. Images / scanned
forms / Excel come later once the pipeline shape is settled and the worker /
DLQ / idempotency mechanics are proven.

### Source URLs to ingest
- https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Denver-City-Council/Press-Room/press-reference/city-council-backgrounder
- https://library.municode.com/co/denver/codes/code_of_ordinances?nodeId=REMUCOCODECO
- https://www.denvergov.org/Government/Legislation-and-Transparency/Transparent-Denver
- https://www.denvergov.org/transparency/checkbook#/home?year=2026
- https://denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Department-of-Finance/Our-Divisions/Budget-and-Management-Office/City-Budget
- https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Department-of-Finance/Financial-Reports
- https://www.denvergov.org/Government/Legislation-and-Transparency/Transparent-Denver/Investments-Debt
- https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Denver-Clerk-and-Recorder/Recording-Division/find-records

---

## How to Frame When Sharing

### For General / Denver Audience (Reddit r/Denver)
> "I built a tool that answers questions about Denver neighborhoods — crime,
> demographics, transit, weather, parks — using city open data. Ask it anything
> about where you live or want to live. Looking for feedback from Denver residents."

- Be upfront that it's a side project
- Invite criticism explicitly
- Respond to every comment — engagement drives sharing
- Post in: r/Denver, r/dataisbeautiful

### For Technical Audience (LinkedIn / r/MachineLearning / r/webdev)
Frame around the technical stack and what you learned:
- Agentic RAG with LangGraph
- Hybrid vector search (dense + sparse BM25) with Qdrant
- Multi-source data integration (GIS, transit, weather, civic data)
- Streaming FastAPI backend + React/Vite frontend on Cloudflare Workers

> "Built an agentic RAG system over Denver's open data catalog using LangGraph,
> Qdrant hybrid search, and FastAPI. The agent routes between a vector store,
> live weather/transit APIs, and a Tavily search tool depending on query intent.
> Here's what I learned building it."

---

## V2 Technical Improvements (Longer Term)

- **Semantic query cache** — Redis-backed cache to avoid redundant LLM calls for similar queries
- **Re-ranker** — metadata boost re-ranker first as a quick quality win on the catalog alone. Cross-encoder becomes load-bearing once the PDF Knowledge Base ships (see that section) — it's the merge step for parallel catalog + KB retrieval, not just polish.
- **Neighborhood alias dictionary** — map colloquial names (RiNo, LoDo, LoHi, Wash Park) to official names
- **CLI tool** — wrap ingest and query scripts into a `bluecypher` CLI for other city datasets
- **User document upload** — personal context layer (lease, insurance policy) cross-referenced with civic data
- **Per-user local vector store** — fully local Qdrant collection for personal documents, nothing leaves the machine
