# Admin API — frontend integration reference

Contract for the PDF Knowledge Base upload flow on the frontend
(`den_bot_frontend`). Keep this doc and `app/admin.py` in sync — if
the API shape changes, both move together.

Backend service: `den_bot_server` (this repo). Endpoints live under
`/admin/*`. The PDF bytes never touch this server — the frontend uploads
directly to Google Cloud Storage via a short-lived signed URL.

---

## High-level flow

```
1. User enters password on /admin login screen
2. Frontend POSTs to /admin/validate-password
3. On 200 → store password in sessionStorage, show upload form
4. User picks PDF + fills metadata (category, title, source URL)
5. Frontend POSTs to /admin/pdf-upload-url with metadata
6. Backend returns { signed_url, object_path, required_headers }
7. Frontend PUTs the PDF bytes to signed_url with required_headers verbatim
8. GCS 200 OK → upload visible to user as "uploaded"
9. (server-side, invisible to frontend) GCS → Pub/Sub → worker → Qdrant
```

---

## Auth model

- **Single shared password** in the `ADMIN_PASSWORD` env on the server.
  No JWT, no token, no session. The password IS the session.
- **Frontend stores password in `sessionStorage`**, not `localStorage`.
  Tab close clears it. (`localStorage` persists across tabs and is
  reachable from any XSS — strictly worse for a shared secret.)
- **Every admin call** includes the password as an `X-Admin-Password`
  header. The server constant-time compares (`hmac.compare_digest`).
- **Rate limit**: 5 admin requests per minute per IP. Hitting it
  returns `429`.

```ts
// Suggested frontend helper
function adminHeaders(): HeadersInit {
  const password = sessionStorage.getItem("admin_password");
  return password ? { "X-Admin-Password": password } : {};
}
```

---

## Endpoint 1 — `POST /admin/validate-password`

Stateless login check. Lets the frontend decide whether to show the
admin UI without inventing a session concept.

### Request

```
POST /admin/validate-password
X-Admin-Password: <user-entered password>
```

No body.

### Response — 200

```json
{ "ok": true }
```

### Status codes

| Code | Meaning | Frontend UX |
|---|---|---|
| `200` | Password correct | Store in sessionStorage; show admin UI |
| `401` | Wrong / missing password | Re-prompt; clear sessionStorage |
| `503` | `ADMIN_PASSWORD` env not set on server | Show generic "admin unavailable" |

---

## Endpoint 2 — `POST /admin/pdf-upload-url`

Validates metadata and returns a 10-minute v4 GCS signed URL with the
metadata bound into required headers.

### Request

```
POST /admin/pdf-upload-url
X-Admin-Password: <password>
Content-Type: application/json

{
  "category": "ordinance",
  "document_title": "Denver Code of Ordinances",
  "original_filename": "Municode Denver CO.pdf",
  "source_url": "https://library.municode.com/co/denver/codes/code_of_ordinances"
}
```

#### Field rules

| Field | Type | Constraints |
|---|---|---|
| `category` | enum | One of: `ordinance` / `council` / `budget` / `finance` / `transparency` / `general`. Server-side allowlist; arbitrary values → 422. |
| `document_title` | string | 1–300 chars. Used in citations like *"Denver Code of Ordinances, pages 11–14"*. |
| `original_filename` | string | 1–300 chars. The literal file name from the picker — preserved as audit metadata; the GCS object path always normalizes to `.pdf`. |
| `source_url` | URL | Must be a valid `http://` or `https://` URL. Used in citations and for "where did this come from" debugging. |

Extra fields are rejected (`extra="forbid"` on the pydantic schema) → 422.

### Response — 200

```json
{
  "signed_url": "https://storage.googleapis.com/den-bot-pdf-uploads/pdfs/ordinance/2026-05-26T143022-municode-denver-co.pdf?X-Goog-Signature=...&...",
  "object_path": "pdfs/ordinance/2026-05-26T143022-municode-denver-co.pdf",
  "required_headers": {
    "Content-Type": "application/pdf",
    "x-goog-meta-document_id": "pdfs/ordinance/2026-05-26T143022-municode-denver-co.pdf",
    "x-goog-meta-document_title": "Denver Code of Ordinances",
    "x-goog-meta-original_filename": "Municode Denver CO.pdf",
    "x-goog-meta-category": "ordinance",
    "x-goog-meta-source_url": "https://library.municode.com/co/denver/codes/code_of_ordinances",
    "x-goog-meta-uploaded_at": "2026-05-26T14:30:22.123456+00:00"
  }
}
```

The `required_headers` are everything you need on the PUT. Spread them
verbatim — don't transform or filter. **If you send different values or
omit any header, GCS returns 403** (that's the tamper-proofing
mechanism preventing the client from rewriting `category` or
`document_title` between issuance and upload).

### Status codes

| Code | Meaning | Frontend UX |
|---|---|---|
| `200` | Signed URL issued | Proceed to step 7 (PUT to GCS) |
| `401` | Wrong / missing password | Re-prompt; clear sessionStorage |
| `422` | Invalid body (bad category, empty title, bad URL, extra field) | Show field-level validation errors |
| `429` | Rate limit hit (5/min/IP) | Show "slow down — try again in a minute" |
| `500` | Signing failed server-side | Show generic error; check server logs |
| `503` | `ADMIN_PASSWORD` or `GCS_UPLOAD_BUCKET` env unset | Show generic "admin unavailable" |

---

## The PUT to GCS (call 2)

Take `signed_url` and `required_headers` from call 1 and PUT the PDF bytes:

```ts
async function uploadPdfToSignedUrl(
  file: File,
  signedUrl: string,
  requiredHeaders: Record<string, string>,
): Promise<void> {
  const response = await fetch(signedUrl, {
    method: "PUT",
    headers: requiredHeaders,   // spread verbatim — no logic
    body: file,
  });
  if (!response.ok) {
    throw new Error(
      `GCS upload failed: ${response.status} ${await response.text()}`,
    );
  }
}
```

### Status codes from GCS

| Code | Meaning | Frontend UX |
|---|---|---|
| `200` / `201` | PDF uploaded; ingestion will start asynchronously | Show "uploaded successfully" |
| `403` | Headers don't match what was signed, or URL expired (>10 min) | Tell user to retry from the start |
| `5xx` | GCS transient failure | Retry once; if it fails again, show error |

---

## What happens after the PUT (server-side, invisible to frontend)

1. GCS finalizes the object and fires a Pub/Sub notification.
2. Pub/Sub pushes the notification to the worker service
   (`den_bot_worker`, separate Cloud Run).
3. Worker downloads the PDF, parses with `pymupdf`, splits into
   parent + child chunks, embeds (dense + sparse BM25), upserts to
   the Qdrant `denver_pdf_knowledge_base` collection.
4. Failures are retried by Pub/Sub up to `max_delivery_attempts`,
   then go to the dead-letter topic for inspection.

**The frontend has no direct visibility into this pipeline today.** The
user uploads, sees a 200, and that's it. To verify a document made it
in, query Blue Cypher after ~30 seconds and confirm the new document
shows up in cited sources.

---

## Future enhancements (not implemented; flag if needed)

- **GET /admin/uploaded-documents** — list of docs currently in the KB
  collection (Qdrant filter query, deduped by `document_id`). Lets the
  admin UI show a "what's uploaded" panel.
- **GET /admin/ingestion-status/{document_id}** — poll for ingestion
  completion. Requires the worker to emit status events (Firestore? a
  new Qdrant `status` field?) so the API can answer.
- **DELETE /admin/document/{document_id}** — delete a doc and all its
  chunks. Qdrant filter-delete by `document_id`; easy to implement once
  the listing endpoint exists.

For v1, none of these are wired. The upload form is a one-way street;
the user verifies success by querying Blue Cypher after the upload.

---

## Suggested frontend component sketch

```ts
// AdminUpload.tsx (rough shape)

async function handleUpload(file: File, metadata: UploadMetadata) {
  // Call 1
  const res1 = await fetch("/admin/pdf-upload-url", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...adminHeaders(),
    },
    body: JSON.stringify(metadata),
  });
  if (res1.status === 401) {
    sessionStorage.removeItem("admin_password");
    setError("Session expired — please log in again");
    return;
  }
  if (!res1.ok) {
    setError(await res1.text());
    return;
  }
  const { signed_url, required_headers } = await res1.json();

  // Call 2
  try {
    await uploadPdfToSignedUrl(file, signed_url, required_headers);
    setSuccess("Uploaded. Ingestion is running in the background.");
  } catch (err) {
    setError(`Upload failed: ${err}`);
  }
}
```

---

## Cross-references

- Backend implementation: `app/admin.py`
- Tests / contract examples: `tests/test_admin.py`
- Worker side of the pipeline: `worker/main.py` + `worker/pipeline/`
- Full design rationale: `ITERATION_V2.md § PDF Knowledge Base`
