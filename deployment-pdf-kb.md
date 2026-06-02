# Deployment Runbook — PDF Knowledge Base (admin upload + worker ingestion)

Console + `gcloud` setup for the V2 PDF Knowledge Base feature on project
**`blue-cypher`**, region **`us-east4`** (matches the existing app + Qdrant Cloud).

This is the infra companion to:
- **`docs/admin-api.md`** — the frontend contract for the upload flow.
- **`ITERATION_V2.md § PDF Knowledge Base`** — full design rationale.
- **`deployment.md`** — the original app-service runbook (reuse its IAM/secret patterns).

There are **two independent tracks** that meet only at the shared GCS bucket:

- **Track A — Admin upload endpoints** (the *existing* `den-bot-server` app
  service): makes `/admin/validate-password` + `/admin/pdf-upload-url` work so
  the frontend can log in and push PDFs into the bucket.
- **Track B — Worker ingestion** (a *new* `den-bot-worker` Cloud Run service):
  consumes GCS → Pub/Sub events and ingests PDFs into Qdrant.

Track A alone gets PDFs into the bucket (they sit there unprocessed). Track B
makes them queryable. Do **Phase 0 first** (both tracks need the bucket), then
A and B can proceed in any order.

> **Console vs. gcloud:** every step below lists both a **Console path** and a
> copy-paste `gcloud` command. Two steps have **no console equivalent** and are
> gcloud-only by necessity — they're flagged inline: bucket **CORS** (no CORS
> editor in the GCS console) and the GCS→Pub/Sub **bucket notification** (no
> console UI binds a bucket's `OBJECT_FINALIZE` to a topic).

## Architecture

```
Frontend (bluecypher.ai)
   │ 1. POST /admin/validate-password        ┐
   │ 2. POST /admin/pdf-upload-url            │  Track A: den-bot-server (app/admin.py)
   └──────────────► den-bot-server ───────────┘  signs v4 URL against the bucket
                         │ returns { signed_url, required_headers }
   3. PUT bytes ◄────────┘
   └──────────────► GCS bucket: den-bot-pdf-uploads
                         │ OBJECT_FINALIZE                        ┐
                         └──► Pub/Sub topic: den-bot-pdf-events    │  Track B
                                   │ push (OIDC)                   │  den-bot-worker
                                   └──► den-bot-worker /pubsub/pdf-ingest
                                             │ parse → chunk → embed → upsert
                                             └──► Qdrant: denver_pdf_knowledge_base
                                   │ (after max_delivery_attempts)
                                   └──► DLQ topic: den-bot-pdf-dlq
```

| Resource | Name | Notes |
|---|---|---|
| GCS bucket | `den-bot-pdf-uploads` | us-east4, uniform access, private. Matches `GCS_UPLOAD_BUCKET` in `cloudbuild.yaml`. |
| Secret (app) | `ADMIN_PASSWORD` | shared admin secret; constant-time compared in `app/admin.py` |
| Cloud Run (worker) | `den-bot-worker` | us-east4, **not** public; only Pub/Sub may invoke |
| Worker image | `den-bot-worker` | in the existing `den-bot` Artifact Registry repo |
| Pub/Sub topic | `den-bot-pdf-events` | GCS notifications land here |
| Pub/Sub DLQ topic | `den-bot-pdf-dlq` | poison PDFs after N failed deliveries |
| Push subscription | `den-bot-pdf-ingest-sub` | pushes to the worker |
| DLQ subscription | `den-bot-pdf-dlq-sub` | retains dead messages for inspection |
| Push auth SA | `den-bot-pubsub-push` | Pub/Sub mints OIDC token as this SA |

> Throughout, `<PROJECT_NUMBER>` is the `blue-cypher` project number (IAM &
> Admin → Settings). The default Cloud Run **runtime SA** is
> `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com` — both the app and
> the worker run as it unless you assign a dedicated SA (see hardening notes).

---

## Phase 0 — Shared: GCS bucket (do this first)

### 0.1 Enable APIs

**Console:** **APIs & Services** → **Enabled APIs & Services** → **+ Enable APIs and Services**. Search and enable any not already on:
- Cloud Storage API
- Pub/Sub API
- **IAM Service Account Credentials API** (`iamcredentials.googleapis.com`) — **required** for keyless v4 URL signing in Track A. Easy to miss.

**gcloud:**
```bash
gcloud services enable storage.googleapis.com pubsub.googleapis.com \
  iamcredentials.googleapis.com --project=blue-cypher
```

### 0.2 Create the bucket

**Console:** **Cloud Storage** → **Buckets** → **Create** → name `den-bot-pdf-uploads`, region `us-east4`, **uniform** access control, **enforce public access prevention**.

**gcloud:**
```bash
gcloud storage buckets create gs://den-bot-pdf-uploads \
  --project=blue-cypher \
  --location=us-east4 \
  --uniform-bucket-level-access \
  --public-access-prevention
```

### 0.3 Set bucket CORS (gotcha — the browser PUT fails without it)

> **gcloud-only:** the GCS console has no CORS editor; CORS is set via the API / `gcloud` / a JSON file.

The frontend PUTs **directly from the browser** to GCS using the signed URL. A
signed URL does **not** bypass browser CORS — GCS must return CORS headers or
the preflight fails. Every `x-goog-meta-*` header the API binds into the
signature must be listed here, or the preflight rejects them.

The policy is version-controlled at **`gcs-cors.json`** in the repo root:

```json
[
  {
    "origin": [
      "https://bluecypher.ai",
      "https://www.bluecypher.ai",
      "http://localhost:5173"
    ],
    "method": ["PUT"],
    "responseHeader": [
      "Content-Type",
      "x-goog-meta-document_id",
      "x-goog-meta-document_title",
      "x-goog-meta-original_filename",
      "x-goog-meta-category",
      "x-goog-meta-source_url",
      "x-goog-meta-uploaded_at"
    ],
    "maxAgeSeconds": 3600
  }
]
```

Apply it (run from the repo root):

```bash
gcloud storage buckets update gs://den-bot-pdf-uploads --cors-file=gcs-cors.json
```

> Keep `gcs-cors.json`'s `responseHeader` list in sync with `required_headers`
> in `app/admin.py` / `docs/admin-api.md`. If you add a metadata field, add the
> header here too, and re-run the command above.

---

## Track A — Admin upload endpoints (app service)

The code is already wired: `cloudbuild.yaml` now sets `GCS_UPLOAD_BUCKET=den-bot-pdf-uploads` (env) and `ADMIN_PASSWORD=ADMIN_PASSWORD:latest` (secret). You just need the secret + IAM, then redeploy.

### A.1 Create the `ADMIN_PASSWORD` secret

Generate a strong password (e.g. `openssl rand -base64 24`).

**Console:** **Security** → **Secret Manager** → **+ Create Secret** → name `ADMIN_PASSWORD`, paste the value into **Secret value**, **automatic** replication, **Create**.

**gcloud:**
```bash
printf '%s' 'YOUR-STRONG-PASSWORD' | \
  gcloud secrets create ADMIN_PASSWORD --data-file=- --replication-policy=automatic
```

> ⚠️ **Bake in no surrounding quotes** — and prefer `printf` over the console
> paste box. Secret Manager stores the bytes verbatim; unlike `.env`, it does
> **not** strip surrounding quotes. A value entered as `"hunter2"` becomes the
> literal 9-character string including quotes, and the constant-time compare
> will fail against the password the frontend sends. If you do use the console,
> paste the raw password with no quotes and no trailing newline. (Same gotcha
> that bit the Resend values — see `secret_manager_quotes_gotcha` in memory.)

Grant the runtime SA read access (skip if you did the project-level grant in `deployment.md` A.3):

**Console:** the secret → **Permissions** → **+ Grant Access** → principal = the runtime SA (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) → role **Secret Manager Secret Accessor**.

**gcloud:**
```bash
gcloud secrets add-iam-policy-binding ADMIN_PASSWORD \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### A.2 IAM for v4 signing + object creation

`app/admin.py` signs with `storage.Client()` using Application Default
Credentials. On Cloud Run that's the runtime SA, which has **no local private
key** — so signing happens via the IAM `signBlob` API. That requires:

1. **IAM Service Account Credentials API enabled** (Phase 0.1).
2. The runtime SA holding **`roles/iam.serviceAccountTokenCreator` on itself**:

   **Console:** **IAM & Admin** → **Service Accounts** → click the runtime SA (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) → **Permissions** tab → **Grant Access** → principal = that same SA → role **Service Account Token Creator**.

   **gcloud:**
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     <PROJECT_NUMBER>-compute@developer.gserviceaccount.com \
     --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
     --role="roles/iam.serviceAccountTokenCreator"
   ```

3. The signer's identity must be allowed to **create** the object the signed
   URL points at (a signed URL inherits the signer's permissions):

   **Console:** **Cloud Storage** → bucket `den-bot-pdf-uploads` → **Permissions** tab → **Grant Access** → principal = the runtime SA → role **Storage Object Admin**.

   **gcloud:**
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://den-bot-pdf-uploads \
     --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
   ```

> `objectCreator` is technically enough (object paths are unique, timestamped),
> but `objectAdmin` also covers the worker's read in Track B if both run as the
> same default SA — one grant, both jobs.

### A.3 Redeploy

`cloudbuild.yaml` is already updated, so just push to `main` (or **Cloud Build → Triggers → `den-bot-server-main` → Run**). The new revision picks up `ADMIN_PASSWORD` + `GCS_UPLOAD_BUCKET`.

> **Must include the keyless-signing fix.** `app/admin.py`'s `_generate_signed_url`
> now detects credentials with no private key (Cloud Run's case) and signs via
> the IAM `signBlob` API instead. Without that code, signing 500s on Cloud Run
> regardless of IAM — the `roles/iam.serviceAccountTokenCreator` grant only
> matters once the code actually calls signBlob. Make sure the revision you
> deploy contains this change (it's on `feature/features_v2`).

#### A.3.1 Pre-redeploy: confirm the two signing prerequisites are live

The signBlob path depends on two things you already did (Phase 0.1 + A.2) — but
they're now **load-bearing**, so a missing one is a guaranteed 500. No new
provisioning here; just verify:

**IAM Service Account Credentials API enabled —**
- **Console:** **APIs & Services → Enabled APIs & Services** → search "IAM Service Account Credentials API".
- **gcloud:**
  ```bash
  gcloud services list --enabled \
    --filter="config.name:iamcredentials.googleapis.com" --project=blue-cypher
  ```

**Runtime SA holds Token Creator on itself —**
- **Console:** **IAM & Admin → Service Accounts** → the runtime SA → **Permissions** tab → confirm that same SA appears with role **Service Account Token Creator**.
- **gcloud:**
  ```bash
  gcloud iam service-accounts get-iam-policy \
    <PROJECT_NUMBER>-compute@developer.gserviceaccount.com \
    --flatten="bindings[].members" \
    --filter="bindings.role:roles/iam.serviceAccountTokenCreator" \
    --format="value(bindings.members)"
  ```
  Expect the runtime SA's own address in the output.

### A.4 Smoke test Track A

```bash
SERVICE=https://<den-bot-server-url>

# Wrong password → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST $SERVICE/admin/validate-password \
  -H "X-Admin-Password: nope"

# Correct password → 200 {"ok":true}
curl -s -X POST $SERVICE/admin/validate-password \
  -H "X-Admin-Password: YOUR-STRONG-PASSWORD"

# Issue a signed URL → 200 with signed_url + required_headers
curl -s -X POST $SERVICE/admin/pdf-upload-url \
  -H "X-Admin-Password: YOUR-STRONG-PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{"category":"general","document_title":"Smoke Test","original_filename":"test.pdf","source_url":"https://example.com"}'
```

Then PUT a real PDF to the returned `signed_url` with the `required_headers`
verbatim → expect `200`. Confirm the object appears in the bucket.

**Watch for:**
- `503` on validate-password → `ADMIN_PASSWORD` secret missing or not readable by the runtime SA.
- `500` "failed to generate signed url" → with the keyless-signing fix deployed this should be resolved. If it persists, read the logged signBlob error:
  - *"you need a private key to sign credentials"* → the deployed revision **predates the keyless-signing fix** (redeploy latest).
  - `ACCESS_TOKEN_SCOPE_INSUFFICIENT` → the signing token lacks the `cloud-platform` scope. The code mints a cloud-platform-scoped token for signBlob; seeing this means the revision predates *that* fix (redeploy latest). **Not** an IAM/role issue — no new permissions needed.
  - `PERMISSION_DENIED` on `signBlob` with *"Permission iam.serviceAccounts.signBlob denied"* → this **is** a role issue: A.2's token-creator-on-self grant (or A.3.1's Credentials API) isn't actually in place. Re-verify both.
- `403` on the PUT → `required_headers` weren't sent verbatim, the URL expired (>10 min), or CORS (Phase 0.3) is missing/mismatched.

✅ **After Track A the frontend's admin flow is fully functional.** Uploaded PDFs land in the bucket and wait for Track B.

---

## Track B — Worker ingestion (Pub/Sub + new Cloud Run service)

### B.0 Worker build config — `cloudbuild.worker.yaml` (done)

The worker build config now lives at **`cloudbuild.worker.yaml`** in the repo
root (the app's `cloudbuild.yaml` builds `app/` only, via the root `Dockerfile`).
It mirrors the app config: build → push → deploy, but with the worker's deploy
parameters (private, `2Gi`, concurrency `4`, max `5`) and the env/secrets in B.1.
The worker `Dockerfile` lives at `worker/Dockerfile` and builds against the
**repo root** as context (it does `COPY worker/ ...`), which is why the config
passes `-f worker/Dockerfile .`.

Two ways to use it:

- **(a) `gcloud builds submit --config` (bootstrap / manual)** — runs the same
  config without a trigger. Use this for the **first** deploy: you need the
  worker URL it produces before you can create the push subscription in B.5.
- **(b) Cloud Build trigger on `cloudbuild.worker.yaml` (durable CI path)** —
  redeploys on push, mirroring `deployment.md` Phase E. Wire this once the
  worker stabilizes. **Notes:** `cloudbuild.worker.yaml` must be on `main` before
  a `^main$` trigger can use it (or point the trigger at the feature branch
  temporarily); and add the trigger substitution **`_TAG` = `$COMMIT_SHA`** so
  each CI revision is pinned to its commit (the config defaults `_TAG` to
  `latest`, which is what makes the manual bootstrap in (a) work — `$COMMIT_SHA`
  is empty on a manual `builds submit`).

> The existing app trigger already deploys Cloud Run as the Cloud Build SA, and
> the worker runs as the **same default compute runtime SA** — so the existing
> `run.admin` + `actAs` grants cover the worker too. No new Cloud Build IAM.

### B.1 Build + deploy the worker

**gcloud (bootstrap — option a):** run the versioned config directly. This
builds, pushes, and deploys `den-bot-worker` in one step:

```bash
gcloud builds submit --config cloudbuild.worker.yaml --project=blue-cypher .
```

**Console:** the image is built from a custom-context Dockerfile, which is
awkward in the console — prefer the `gcloud builds submit` above. Once the image
is in Artifact Registry you *can* deploy from the console: **Cloud Run** →
**Deploy container** → **Service** → select `den-bot-worker:latest` → region
`us-east4`, **Require authentication** (not "Allow unauthenticated"), then under
**Container, Variables & Secrets, Connections, Security**: memory `2Gi`, request
timeout `600`, max concurrency `4`, max instances `5`; add the env var + the
three secrets listed below. But `cloudbuild.worker.yaml` already encodes all of
this, so the one-command path is preferred.

The deploy parameters (all baked into `cloudbuild.worker.yaml`):
- **`--no-allow-unauthenticated`** — the worker is internal; only the Pub/Sub push SA may call it (B.5). Never expose `/pubsub/pdf-ingest` publicly.
- **`--memory=2Gi`** — `pymupdf` parsing + 3072-dim embeddings are memory-heavy; bump if you see OOM in logs.
- **`--timeout=600s`** + **`--concurrency=4`** — keep the HTTP timeout ≥ the Pub/Sub ack deadline (B.5), and concurrency low since ingestion is CPU/memory-bound.
- **env `QDRANT_KB_COLLECTION_NAME=denver_pdf_knowledge_base`** + **secrets `GEMINI_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`** — `langchain_google_genai` reads `GEMINI_API_KEY` from the env (same as the app), and the worker writes to the same Qdrant cluster. The collection name defaults to `denver_pdf_knowledge_base` in code; set explicitly for clarity.

> **No new secrets needed.** `GEMINI_API_KEY`, `QDRANT_URL`, and `QDRANT_API_KEY`
> already exist in Secret Manager (created for the app — see `deployment.md`).
> The worker reuses them as-is. The only Secret Manager addition for V2 was
> `ADMIN_PASSWORD`, which belongs to **Track A**, not the worker.

Capture the worker URL (you'll need it in B.5):

**Console:** **Cloud Run** → `den-bot-worker` → the URL shown at the top.

**gcloud:**
```bash
gcloud run services describe den-bot-worker --region=us-east4 --format='value(status.url)'
```

### B.2 Worker IAM

Same console pattern as A.1/A.2 (IAM & Admin or the resource's **Permissions** tab); gcloud below.

- **Secret access**: the runtime SA needs `roles/secretmanager.secretAccessor` (already granted project-wide if you did `deployment.md` A.3; otherwise grant on the three secrets above).
- **Bucket read**: the worker downloads the PDF (`worker/pipeline/gcs.py`). If the worker runs as the default compute SA and you granted `objectAdmin` in A.2, read is already covered. If you use a **dedicated** worker SA, grant it `roles/storage.objectViewer` on `gs://den-bot-pdf-uploads`:

  ```bash
  gcloud storage buckets add-iam-policy-binding gs://den-bot-pdf-uploads \
    --member="serviceAccount:<dedicated-worker-sa>" \
    --role="roles/storage.objectViewer"
  ```

### B.3 Create the topics

**Console:** **Pub/Sub** → **Topics** → **Create Topic** → ID `den-bot-pdf-events` (leave "Add default subscription" unchecked). Repeat for `den-bot-pdf-dlq`.

**gcloud:**
```bash
gcloud pubsub topics create den-bot-pdf-events
gcloud pubsub topics create den-bot-pdf-dlq
```

### B.4 Wire GCS → topic (OBJECT_FINALIZE notification)

> **gcloud-only for the notification itself:** no console UI binds a bucket's
> `OBJECT_FINALIZE` event to a Pub/Sub topic. (The topic IAM grant in the first
> block *can* be done via the topic's **Permissions** tab in the console.)

The Cloud Storage **service agent** must be allowed to publish to the topic:

```bash
# Find the GCS service agent
GCS_SA=$(gcloud storage service-agent --project=blue-cypher)
echo "$GCS_SA"   # service-<PROJECT_NUMBER>@gs-project-accounts.iam.gserviceaccount.com

gcloud pubsub topics add-iam-policy-binding den-bot-pdf-events \
  --member="serviceAccount:$GCS_SA" \
  --role="roles/pubsub.publisher"
```

Create the notification (JSON payload carries the `x-goog-meta-*` values, prefix stripped, in the `metadata` dict — which is what `worker/main.py` reads):

```bash
gcloud storage buckets notifications create gs://den-bot-pdf-uploads \
  --topic=den-bot-pdf-events \
  --event-types=OBJECT_FINALIZE \
  --payload-format=json
```

### B.5 Create the push subscription → worker

Use a dedicated SA so Pub/Sub sends an OIDC token the worker can authorize.

**Console:**
- Create the SA: **IAM & Admin** → **Service Accounts** → **Create** → name `den-bot-pubsub-push`.
- Grant it invoke rights: **Cloud Run** → `den-bot-worker` → **Permissions** (or **Security** tab) → **Add Principal** → that SA → role **Cloud Run Invoker**.
- Create the subscription: **Pub/Sub** → **Subscriptions** → **Create Subscription** → ID `den-bot-pdf-ingest-sub`, topic `den-bot-pdf-events`, **Delivery type: Push**, endpoint `<worker-url>/pubsub/pdf-ingest`, **Enable authentication** → select the `den-bot-pubsub-push` SA, **Acknowledgement deadline** `600`, **Dead lettering: Enable** → topic `den-bot-pdf-dlq`, **Maximum delivery attempts** `5`.

**gcloud:**
```bash
# 1. Dedicated push identity
gcloud iam service-accounts create den-bot-pubsub-push \
  --display-name="Pub/Sub push to den-bot-worker"

PUSH_SA=den-bot-pubsub-push@blue-cypher.iam.gserviceaccount.com

# 2. Let it invoke the (private) worker
gcloud run services add-iam-policy-binding den-bot-worker \
  --region=us-east4 \
  --member="serviceAccount:$PUSH_SA" \
  --role="roles/run.invoker"

# 3. Create the push subscription with DLQ + generous ack deadline
WORKER_URL=$(gcloud run services describe den-bot-worker --region=us-east4 --format='value(status.url)')

gcloud pubsub subscriptions create den-bot-pdf-ingest-sub \
  --topic=den-bot-pdf-events \
  --push-endpoint="${WORKER_URL}/pubsub/pdf-ingest" \
  --push-auth-service-account="$PUSH_SA" \
  --ack-deadline=600 \
  --dead-letter-topic=den-bot-pdf-dlq \
  --max-delivery-attempts=5
```

### B.6 DLQ permissions + inspection subscription

For Pub/Sub to forward dead messages, its **service agent** needs to publish to
the DLQ topic and subscribe to the source subscription.

**Console:** grant on the resource's **Permissions** tab — the DLQ topic
(`den-bot-pdf-dlq` → **Permissions** → add the Pub/Sub service agent
`service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com` as **Pub/Sub
Publisher**) and the subscription (`den-bot-pdf-ingest-sub` → **Permissions** →
same agent as **Pub/Sub Subscriber**). Then create the inspection sub:
**Subscriptions** → **Create** → ID `den-bot-pdf-dlq-sub`, topic
`den-bot-pdf-dlq`, **Pull**.

**gcloud:**
```bash
PUBSUB_SA=service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam.gserviceaccount.com

gcloud pubsub topics add-iam-policy-binding den-bot-pdf-dlq \
  --member="serviceAccount:$PUBSUB_SA" --role="roles/pubsub.publisher"

gcloud pubsub subscriptions add-iam-policy-binding den-bot-pdf-ingest-sub \
  --member="serviceAccount:$PUBSUB_SA" --role="roles/pubsub.subscriber"

# A pull subscription so dead messages are retained for inspection
gcloud pubsub subscriptions create den-bot-pdf-dlq-sub --topic=den-bot-pdf-dlq
```

### B.7 End-to-end smoke test

1. Run the Track A smoke (A.4) to PUT a real PDF into the bucket.
2. Tail worker logs: **Cloud Run → `den-bot-worker` → Logs**. Within seconds expect:
   `ingest received: ...` then `ingest succeeded: document_id=... parents=N children=M`.
3. Query Blue Cypher (`/query`) for something in that PDF after ~30s.
   - ⚠️ Until the **retrieval integration** ships (ITERATION_V2 § Retrieval changes — the retriever doesn't search `denver_pdf_knowledge_base` yet), the doc won't surface in answers. Verify ingestion instead by checking the Qdrant collection point count directly.
4. **Failure path**: pull the DLQ subscription after a few minutes to confirm poison messages land there, not retry forever:
   ```bash
   gcloud pubsub subscriptions pull den-bot-pdf-dlq-sub --auto-ack --limit=5
   ```

**Watch for:**
- Worker `403`/`401` from Pub/Sub never arriving → push SA missing `run.invoker` (B.5 step 2).
- Notifications not firing → GCS service agent missing `pubsub.publisher` (B.4).
- Messages redelivering forever, never hitting DLQ → Pub/Sub service agent missing the B.6 grants.
- Worker OOM / timeout → bump `--memory` / confirm `--timeout` ≥ `--ack-deadline`.

---

## Operations

### Redeploying the worker
Manual: rerun B.1 (`gcloud builds submit --config cloudbuild.worker.yaml .`). CI: a Cloud Build trigger on `cloudbuild.worker.yaml` redeploys on push (B.0 option b).

### Rotating `ADMIN_PASSWORD`
Secret Manager → `ADMIN_PASSWORD` → **+ New Version** (via `printf ... | gcloud secrets versions add ADMIN_PASSWORD --data-file=-`). Redeploy the app to pick up `:latest`. Tell the frontend admin the new password out of band.

### Replaying a dead-lettered PDF
Inspect via `den-bot-pdf-dlq-sub`. To reprocess, re-upload the PDF (simplest — fires a fresh `OBJECT_FINALIZE`) or publish the original message back onto `den-bot-pdf-events`.

### Cost delta over the base app
- Cloud Run worker: scales to zero; ~$0 idle, pay-per-ingest.
- Pub/Sub: free tier covers this volume.
- GCS: pennies for a handful of PDFs.
- Gemini embeddings at ingest time: one-time per document, negligible.

### Hardening (later)
- Give the worker its **own** runtime SA (`den-bot-worker-sa`) with only `objectViewer` on the bucket + `secretAccessor` on its three secrets, instead of the shared compute default SA.
- Restrict the bucket CORS `origin` list to prod only once local dev no longer hits prod.
- Add object lifecycle on the bucket (e.g. delete raw PDFs after N days — the text already lives in Qdrant).

---

## Quick checklist

**Phase 0 (shared):** APIs incl. IAM Credentials · bucket `den-bot-pdf-uploads` · CORS

**Track A:** `ADMIN_PASSWORD` secret (printf, no quotes) · runtime SA: secretAccessor + tokenCreator-on-self + objectAdmin on bucket · redeploy app · smoke

**Track B:** worker build config (`cloudbuild.worker.yaml`, done) · deploy `den-bot-worker` (private, `gcloud builds submit --config`) · worker IAM · topics `den-bot-pdf-events` + `den-bot-pdf-dlq` · GCS service agent publisher · bucket notification · push SA + `run.invoker` · push subscription w/ DLQ · Pub/Sub service agent DLQ grants · DLQ inspection sub · smoke
