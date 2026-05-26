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
- **Worker pipeline:** download PDF from GCS → parse with `pymupdf` → split into parent/child chunks (see "Parsing + chunking strategy" below) → embed children only with the same Google `gemini-embedding-001` as the catalog (so cross-collection retrieval mixes cleanly in vector space) → upsert children with parent text denormalized into the payload.

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

**Category, not collection.** Frontend selects a *category* from a fixed
allowlist which becomes a Qdrant payload field. **Never let the frontend
name the Qdrant collection directly** — that's an injection vector into the
catalog collection. One PDF KB collection with a `category` filter scales
better than N collections: the retriever still fans out across just two
(catalog + KB), and adding a new category is a config change, not a graph
change.

**v1 category allowlist:**
`ordinance` / `council` / `budget` / `finance` / `transparency` / `general`

`general` is the escape hatch for any doc that doesn't fit the named buckets
— prevents the upload form from blocking on a missing category. Add new
categories as needed (server-side allowlist update + frontend select option).

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

### Metadata flow (API → GCS → Pub/Sub → worker)

How upload metadata (category, title, source URL, filename, etc.) actually
reaches the worker. **Key property: the worker has zero coupling to the
API server.** It never calls back; the GCS Pub/Sub notification carries
every field it needs. The carrier mechanism is GCS custom metadata
(`x-goog-meta-*` headers), which GCS persists with the object and
automatically includes in the notification payload.

**Chain:**

```
Frontend form  →  /admin/pdf-upload-url  →  signed URL with required headers
                                          ↓
                  Frontend PUT  →  GCS object + custom metadata attached
                                          ↓
                  GCS object.finalize  →  Pub/Sub notification (metadata in body)
                                          ↓
                  Push to worker  →  Worker reads metadata from message
```

**Step 1 — API binds metadata into the signed URL:**

```python
# In this repo's /admin/pdf-upload-url handler:
document_id = f"pdfs/{category}/{timestamp}-{slug(original_filename)}.pdf"
uploaded_at = datetime.utcnow().isoformat()

custom_metadata = {
    "document_title": document_title,
    "source_url": source_url,
    "category": category,
    "original_filename": original_filename,
    "document_id": document_id,
    "uploaded_at": uploaded_at,
}

signed_url = bucket.blob(document_id).generate_signed_url(
    version="v4",
    method="PUT",
    expiration=timedelta(minutes=10),
    content_type="application/pdf",
    headers={f"x-goog-meta-{k}": v for k, v in custom_metadata.items()},
)

return {
    "signed_url": signed_url,
    "object_path": document_id,
    "required_headers": {
        "Content-Type": "application/pdf",
        **{f"x-goog-meta-{k}": v for k, v in custom_metadata.items()},
    },
}
```

**Step 2 — Frontend PUTs with required_headers verbatim.** GCS persists
the `x-goog-meta-*` values as custom metadata on the object. Any mismatch
between PUT headers and what was signed → 403. That's the tamper-proofing.

**Step 3 — Pub/Sub push message arrives at the worker.** The `data` field
is base64-encoded JSON; decoded it's the full GCS object metadata
including the `metadata` dict (with the `x-goog-meta-` prefix stripped):

```json
{
  "bucket": "your-pdf-bucket",
  "name": "pdfs/ordinance/2026-05-25-municode-denver-co.pdf",
  "contentType": "application/pdf",
  "size": "4823901",
  "metadata": {
    "document_title": "Denver Code of Ordinances",
    "source_url": "https://library.municode.com/...",
    "category": "ordinance",
    "original_filename": "municode-denver-co.pdf",
    "document_id": "pdfs/ordinance/2026-05-25-municode-denver-co.pdf",
    "uploaded_at": "2026-05-25T..."
  }
}
```

**Step 4 — Worker extracts + downloads + processes:**

```python
# In worker's /pubsub/pdf-ingest handler:
envelope = await request.json()
gcs_event = json.loads(base64.b64decode(envelope["message"]["data"]))

bucket_name = gcs_event["bucket"]
object_name = gcs_event["name"]
custom = gcs_event["metadata"]  # all our fields, prefix already stripped

pdf_bytes = (
    storage_client.bucket(bucket_name).blob(object_name).download_as_bytes()
)
# Parse → chunk → embed → upsert; custom["document_title"], custom["category"],
# etc. go straight into each Qdrant point's payload.
```

**Pub/Sub notification configuration (one-time):**

```bash
gcloud storage buckets notifications create gs://your-pdf-bucket \
  --topic=pdf-ingest-topic \
  --event-types=OBJECT_FINALIZE \
  --payload-format=json
```

`--payload-format=json` is the version that includes the full object JSON
(with the `metadata` dict) in the message body. The thinner format would
force the worker to call back to GCS for metadata — defeating the whole
"the notification is the message" property.

**Implications worth keeping in mind:**

- **No metadata-passing endpoint** between services. No "tell the worker about this new doc" call. The Pub/Sub notification IS the message.
- **No callback path.** API can be fully down and ingestion still works (worker only depends on GCS + Pub/Sub + Qdrant + the embedding API). API outage stops *new* uploads being initiated; it doesn't stop in-flight ones from completing.
- **Metadata key naming:** stick to `snake_case` to avoid header-case confusion. GCS preserves the keys after the prefix as-is.

### Parsing + chunking strategy

**Parser:** `pymupdf`. Faster than `pypdf`, handles rotated text, weird
encodings, and embedded fonts better. AGPL-3.0 license — fine for this
project, would matter for closed-source commercial relicensing. Import is
`import pymupdf` (the legacy `fitz` name still works).

**Chunking: parent/child via `RecursiveCharacterTextSplitter`** — small
chunks for precise vector matching, larger chunks returned to the generator
for context. The standard "match small, return big" pattern.

**Params (v1):**
- **Parent**: ~1500 tokens, ~150 token overlap (~10%). Big enough to hold a coherent section.
- **Child**: ~300 tokens, ~50 token overlap. Small enough that the cross-encoder can score them precisely.
- Each parent splits into 3–5 children that carry a stable `parent_index` linking back.
- Token-based, not character-based — use a tokenizer matching the embedding model.
- Recursive splitter tries paragraph → sentence → word → character boundaries before falling back to a hard cut.

**Storage shape: single Qdrant collection, parent text denormalized into
child payload.** Children are the only points indexed; parents ride along in
the payload. One Qdrant call returns everything the generator needs.

```python
{
    "id": deterministic_uuid(document_id, child_index),
    "vector": embed(child_text),  # only children get embedded
    "payload": {
        # Document-level (constant across all children of this doc)
        "document_id": "pdfs/ordinance/2026-05-25-municode.pdf",
        "document_title": "Denver Code of Ordinances",     # admin-edited, used in citations
        "original_filename": "municode-denver-co.pdf",     # literal upload, audit-grade provenance
        "category": "ordinance",
        "source_url": "https://library.municode.com/...",
        "source_collection": "knowledge_base",
        "uploaded_at": "2026-05-25T...",

        # Child-level
        "child_index": 7,
        "child_text": "...",          # the ~300-token chunk that was embedded
        "child_start_page": 12,
        "child_end_page": 12,         # child can span page breaks on dense pages

        # Parent-level (denormalized — duplicated across siblings)
        "parent_index": 2,            # multiple children share this
        "parent_text": "...",         # the ~1500-token surrounding chunk
        "parent_start_page": 11,
        "parent_end_page": 14,
    }
}
```

**Why both `document_title` and `original_filename`:** `document_title` is
admin-edited at upload time and can be wrong, abbreviated, or changed later.
`original_filename` is the literal file the worker processed and is the only
fully reliable provenance for "where did this chunk really come from."
Citations use `document_title`; debugging and audit use `original_filename`.

**Why single collection (not two):**
- One Qdrant round-trip per query, not two.
- One upsert path in the worker; one delete-by-document_id path.
- No schema-drift risk between two collections that have to stay in sync.
- Parent text duplication across siblings is ~50–100 MB total at this scale — Qdrant doesn't care.
- Matches LangChain's `ParentDocumentRetriever` pattern conceptually.

The clean argument for two collections would be querying parents directly
as a separate path (e.g., "summarize the whole budget doc"). Not on the
roadmap, so the second collection is overhead with no payoff.

**Retrieval flow with parent/child:**

```
1. Embed query → Qdrant similarity search on children → top-20
2. Cohere rerank against child_text → ranked list
3. Dedupe by (document_id, parent_index), keep highest-ranked child per parent
4. Take top-5 unique parents
5. Pass parent_text to the generator with citation = document_title + parent_start_page–parent_end_page
```

**Citation style: parent-range, not child-page.** Render as
`"Denver Code of Ordinances, pages 11–14"`. Reflects the actual scope the
LLM reasoned over. Citing the child page (`"page 12"`) looks more precise
but is misleading — the model saw the entire parent, not just the child.
Don't claim more precision than you have.

**Why rerank on children, not parents:**
- Children are the granularity that matched — let the cross-encoder exploit precise matching.
- Reranking 20 × 1500-token parents would be slow and might hit token limits.
- Parent expansion is purely for the generator's context, not for ranking.

**Why dedupe after rerank, not before:** if children A and B both come from
parent X, let the reranker score them independently — whichever scores
higher tells you which part of parent X best matched the query. Then
collapse.

**Honest trade-offs accepted:**
- Municipal code has structure (titles/chapters/sections) that recursive splitting loses. A clause about noise might end up in a parent labeled by a previous chapter heading. Acceptable for v1; structure-aware parsing for municode is a possible later upgrade.
- Financial reports with tables: splitter can break a table mid-row. Acceptable for v1; if FY-report query quality suffers, table-aware extraction is the upgrade.
- Semantic chunking was considered and skipped: marginal quality gain, real ingest-time cost (extra embedding calls), uneven chunk sizes that complicate retrieval. Not worth it.

### Retrieval changes
Catalog and PDF KB get searched **in parallel** per query, then merged before
grading. A question about "Denver zoning" could legitimately want both the
catalog (the GIS layer) *and* the zoning PDF.

- **Retriever node** runs both collections via `asyncio.gather` — don't serialize or end-to-end latency doubles. KB side returns child hits with parent text already in payload (see Parsing + chunking strategy).
- **Reranker node becomes load-bearing**, not just a polish pass. It's the merge-and-reorder step on a mixed-provenance list. **Cohere `rerank-english-v3.0`** — managed API, no infra, free tier covers our volume, ~100–300ms for top-20 candidates. Reranks against child_text (PDF) and full chunk text (catalog) uniformly; parent expansion happens after rerank on the KB side.
- **Per-doc provenance metadata** — every chunk carries `source_collection: "catalog" | "knowledge_base"`. Catalog hits cite via `hub_url`; PDF hits cite via `document_title` + `parent_start_page`–`parent_end_page` range from the denormalized parent payload (see Parsing + chunking strategy for why range, not single page).
- **No `main_router` change needed** — `data_search` continues to mean "RAG path"; the retriever node decides what that means internally.

### Repo layout: monorepo with separate worker service

The worker lives in a **`worker/` subdirectory of this repo**, not a
separate repository. Deployment is still two distinct Cloud Run services
(separate Dockerfiles, separate `gcloud run deploy` invocations), so the
production-lessons isolation — separate scaling, failure domains, logs,
cold starts — is fully preserved. What you get from monorepo:

- **Atomic contract changes.** Editing the GCS object path convention, the metadata header keys, or the Qdrant payload schema can land in a single PR that touches both `app/` (signer + retriever) and `worker/` (parser + upserter). No cross-repo PR ceremony, no risk of one side merging while the other lags.
- **One CI config.** Single test suite covers both. Shared schema duplication is fine for v1; if/when it starts drifting, promote shared types into an `app/shared/` package both sides import.
- **One dev loop.** `docker compose up` brings up Qdrant + Redis + (eventually) a local Pub/Sub emulator + both services. No cloning two repos to run end-to-end.

**Suggested structure:**

```
den_bot_server/
├── app/                    # existing FastAPI (query API + admin endpoints)
│   ├── graph/
│   ├── tools/
│   ├── main.py
│   └── ...
├── worker/                 # new FastAPI for Pub/Sub push
│   ├── main.py             # /pubsub/pdf-ingest endpoint
│   ├── pipeline/           # pymupdf parse + chunk + embed + upsert
│   └── Dockerfile
├── shared/                 # (later) Qdrant payload types, path conventions
├── Dockerfile              # existing — for app/
├── pyproject.toml          # one dependency tree, or split if it becomes bloated
└── docker-compose.yml      # local dev: qdrant + redis + (later) pubsub emulator
```

**The blast-radius concern** (a worker bug crashing API deploys) is avoided
by keeping separate Dockerfiles and separate Cloud Run services — the
shared repo just means shared code review and shared CI, not shared
runtime.

**Upgrade path to two repos** if the shared dependency tree starts feeling
bloated or the worker needs a fundamentally different runtime (GPU, larger
container, different base image). Not a v1 concern.

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
