# Knowledge Base API — frontend integration reference

Contract for the **public** read side of the PDF Knowledge Base — the
frontend 'About' page that lists ingested documents and lets users download
them. Keep this doc and `app/knowledge_base.py` in sync.

Backend service: `den_bot_server` (this repo). Endpoints live under
`/knowledge-base/*`. **No auth** — these are public reads (document titles,
sources, and civic-PDF downloads aren't sensitive). The admin *upload* flow is
separate and password-protected — see `admin-api.md`.

Source of truth is Qdrant (what's actually queryable), not the GCS bucket.

---

## `GET /knowledge-base/documents`

Lists the distinct documents in the knowledge base, deduped by `document_id`,
newest first. Drives the About-page list. Returns `{"documents": []}` before
anything is ingested.

**Response 200:**
```jsonc
{
  "documents": [
    {
      "document_id": "pdfs/budget/2026-06-08T143022-budget-highlights.pdf",
      "document_title": "2026 Budget Highlights",
      "category": "budget",                  // nullable
      "source_url": "https://denvergov.org/.../budget.pdf",  // nullable
      "original_filename": "budget.pdf",     // nullable
      "uploaded_at": "2026-06-08T14:30:22+00:00",            // nullable, ISO-8601
      "num_chunks": 42,
      "page_count": 18                        // approx (max parent page covered)
    }
  ]
}
```

**Rendering:**
- Title = `document_title`. `category` is a good tag/badge.
- **`source_url`** → render as a **"View on denvergov.org"** link (the canonical,
  always-current version). It's **nullable** — omit the link when absent.
- **Download** → call the endpoint below with this entry's `document_id`.

## `GET /knowledge-base/documents/download?document_id=<id>`

Issues a short-lived (~15 min) signed GET URL for the exact PDF we indexed —
so a download always matches the page citations in answers. Call it **on click**
(don't prefetch for the whole list; the URLs expire).

**Query params:** `document_id` — the value from the list above (the GCS object
path). Pass it as a **query param**, not a path segment (it contains slashes).

**Response 200:**
```jsonc
{
  "document_id": "pdfs/budget/2026-06-08T143022-budget-highlights.pdf",
  "download_url": "https://storage.googleapis.com/...&X-Goog-Signature=..."
}
```

Open `download_url` directly — `window.open(url)` or an `<a href={url}>`. It's a
**top-level GET**, so no CORS is involved. (Only a `fetch()` of the URL would
need GET added to the bucket CORS policy — avoid; just open it.)

**Errors:**
| Status | When |
|---|---|
| `400` | `document_id` doesn't start with `pdfs/` (guards against signing arbitrary objects) |
| `404` | the object no longer exists in storage |
| `422` | `document_id` query param missing |
| `503` | `GCS_UPLOAD_BUCKET` not configured server-side |

---

## In-chat downloads (the `/query` `sources` event)

The download endpoint isn't just for the About page — KB documents cited in a
chat answer can be downloaded inline too. When `/query` retrieves a PDF, its
entry in the streamed `sources` event carries the same `document_id`:

```jsonc
// one entry in the `sources` SSE event, for a knowledge-base hit
{
  "source_collection": "knowledge_base",   // discriminator
  "document_id": "pdfs/budget/2026-...-budget.pdf",   // → pass to /download
  "document_title": "2026 Budget Highlights",
  "source_url": "https://denvergov.org/.../budget.pdf",  // optional
  "page_start": 11,                          // optional
  "page_end": 14,                            // optional
  "category": "budget"                       // optional
}
```

So the same on-click flow works in both places: take `document_id` →
`GET /knowledge-base/documents/download?document_id=<id>` → open `download_url`.
(Catalog/GIS source entries have `service_name` + `base_url` instead and no
`source_collection`; branch on `source_collection === "knowledge_base"`.)

---

## Why two links (Download vs. View on denvergov.org)

They serve different jobs:
- **Download** (signed URL) → the **exact artifact we ingested**. Page citations
  like "pp. 11–14" always match it, even if the city later updates their copy.
  Works for every document, including those uploaded without a `source_url`.
- **View on denvergov.org** (`source_url`) → the **canonical, current** version
  on the city site. May change/move over time (city links rot — that's why we
  don't rely on them for downloads), but it's the authoritative public source.
