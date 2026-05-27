# Deployment Runbook — den_bot_server

Console-driven setup for project **`blue-cypher`** in region **`us-east4`** (matches the Qdrant Cloud cluster).

## Architecture

```
GitHub (main push)
   └── Cloud Build trigger ── cloudbuild.yaml
            └── builds image → Artifact Registry (us-east4)
                       └── deploys → Cloud Run service "den-bot-server"
                                          │
                                          └── public TLS: Gemini, Qdrant Cloud, Redis Cloud,
                                                          RTD feeds, Tavily, Resend
                                          
                                  Secret Manager: GEMINI_API_KEY, QDRANT_*, REDIS_URL, RESEND_*, LANGCHAIN_*, TAVILY_API_KEY
```

> **Why no VPC?** `langgraph-checkpoint-redis` requires the RedisJSON + RediSearch modules. **Redis Cloud running Redis 8** bundles both into core and exposes a public TLS endpoint — no VPC needed. Memorystore and Upstash were tried first and both failed; see Phase C for the full story.

| Resource | Name | Region |
|---|---|---|
| Artifact Registry repo | `den-bot` | us-east4 |
| Cloud Run service | `den-bot-server` | us-east4 |
| Redis Cloud database | `den-bot-redis` | AWS us-east-1 (Virginia, closest to us-east4) |
| Build trigger | `den-bot-server-main` | global |

---

## Phase A — Enable APIs + IAM

### A.1 Enable APIs
Console → **APIs & Services** → **Enabled APIs & Services** → **+ Enable APIs and Services**. Search and enable:

- Cloud Run Admin API
- Cloud Build API
- Artifact Registry API
- Secret Manager API
- Compute Engine API (for the default runtime SA)

Wait until all show "Enabled".

### A.2 Grant Cloud Build SA the deploy permissions

The Cloud Build default service account is `<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`. Find the project number under **IAM & Admin** → **Settings**.

Console → **IAM & Admin** → **IAM**. If the Cloud Build SA isn't listed, click **Grant Access** and add the principal. Then assign these roles to it:

- **Cloud Run Admin** (`roles/run.admin`) — deploy revisions
- **Service Account User** (`roles/iam.serviceAccountUser`) — needed to "act as" the Cloud Run runtime SA during deploy
- **Artifact Registry Writer** (`roles/artifactregistry.writer`) — push images
- **Secret Manager Secret Accessor** (`roles/secretmanager.secretAccessor`) — read secrets during validation (Cloud Run does this on the runtime SA, but Cloud Build inspects them at deploy)
- **Logs Writer** (`roles/logging.logWriter`) — required because cloudbuild.yaml uses `options.logging: CLOUD_LOGGING_ONLY`

### A.3 Grant Cloud Run runtime SA secret-read permission

The Cloud Run **runtime** SA defaults to the Compute Engine default SA: `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`. Grant it:

- **Secret Manager Secret Accessor** (`roles/secretmanager.secretAccessor`) — read secrets at container startup

You can grant this on individual secrets in Phase D for tighter scope, but a project-level grant is simpler. Choose either.

> If you also granted `roles/vpcaccess.user` based on an earlier version of this runbook (when we used Memorystore via a VPC connector), it's harmless to leave but can be removed — we no longer use a VPC.

---

## Phase B — Artifact Registry

Console → **Artifact Registry** → **Repositories** → **+ Create Repository**.

- Name: **`den-bot`**
- Format: **Docker**
- Mode: **Standard**
- Location type: **Region**, **us-east4**
- Encryption: Google-managed (default)
- Immutable image tags: off (we want to overwrite `:latest`)

Done. The image will land at `us-east4-docker.pkg.dev/blue-cypher/den-bot/den-bot-server`.

---

## Phase C — Redis Cloud (Redis 8)

`langgraph-checkpoint-redis` requires the RediSearch + RedisJSON modules. **Redis 8** bundles both into core, so any Redis Cloud database running v8 has them available by default — no module checkboxes needed.

### C.1 Provision the database

1. Sign up / log in at **https://redis.com/try-free/**
2. In the Redis Cloud Console, choose **Essentials** → **Free** (30 MB).
3. Create the database:
   - **Name**: `den-bot-redis`
   - **Cloud vendor**: AWS
   - **Region**: `us-east-1 (N. Virginia)` — closest to GCP us-east4
   - **Redis version**: **8** (modules are built in; no separate selection required)
   - **High availability**: Off (free tier)
   - **Data persistence**: leave at default (free tier may force off; fine — checkpoints have TTL)
4. **Activate** / **Create**

Provisioning takes ~30 seconds.

### C.2 Capture the connection string

Open the database → **Configuration** tab → **General** section.

- **Public endpoint**: `redis-XXXXX.cNN.region.cloud.redislabs.com:PORT`
- **Default user password**: click the eye icon to reveal

Build the URL (TLS is on by default on Redis Cloud):

```
rediss://default:<password>@redis-XXXXX.cNN.region.cloud.redislabs.com:PORT
```

This value goes into the `REDIS_URL` secret in Phase D.

### C.3 Verify modules before deploying

This is the step we skipped on the previous two attempts. Confirm `FT._LIST` works against the new DB:

```bash
redis-cli -u "rediss://default:<password>@redis-XXXXX.cNN.region.cloud.redislabs.com:PORT" FT._LIST
# → (empty array)  ← good: module loaded, no indexes yet
```

If you get `(error) ERR unknown command 'FT._LIST'`, the modules aren't available — recheck that the database is Redis 8.

Also verify JSON:

```bash
redis-cli -u "rediss://..." JSON.SET test '$' '{"ok": true}'
# → OK
```

If both pass, the DB is compatible. Update `REDIS_URL` in Secret Manager (new version) and re-trigger Cloud Build.

---

## Phase D — Secret Manager

Console → **Security** → **Secret Manager** → **+ Create Secret**.

Create one secret per env var below. For each: name (exactly as listed), paste the value, **Add Secret**. Region: **automatic / global replication** (simplest).

| Secret name | Value source |
|---|---|
| `GEMINI_API_KEY` | from your local `.env` |
| `QDRANT_URL` | `https://a034457f-75ff-486d-b27e-225898eecacb.us-east4-0.gcp.cloud.qdrant.io` (from `.env.production`) |
| `QDRANT_API_KEY` | from `.env.production` |
| `REDIS_URL` | `rediss://default:<password>@redis-XXXXX.cNN.region.cloud.redislabs.com:PORT` (from Phase C.2) |
| `RESEND_API_KEY` | from `.env` |
| `FEEDBACK_TO_EMAIL` | your inbox |
| `FEEDBACK_FROM_EMAIL` | sender address |
| `LANGCHAIN_API_KEY` | from `.env` |
| `TAVILY_API_KEY` | from `.env` |

If you skipped the project-level `secretmanager.secretAccessor` grant in A.3, repeat per-secret here: each secret → **Permissions** → **+ Add Principal** → Cloud Run runtime SA → **Secret Manager Secret Accessor**.

> **Note on `ALLOWED_ORIGINS`**: not sensitive; baked into `cloudbuild.yaml` as a `--set-env-vars` value (`https://bluecypher.ai,https://www.bluecypher.ai`). If origins change, edit `cloudbuild.yaml` and merge.

---

## Phase E — Cloud Build trigger (GitHub → main)

### E.1 Connect the GitHub repo

Console → **Cloud Build** → **Triggers** → **Connect Repository**.

- Source: **GitHub (Cloud Build GitHub App)**
- Authenticate, install the Cloud Build GitHub App on `uelski/den_bot_server` (or whichever org/repo holds it), grant repo access.
- Pick the repo. **Connect**.

### E.2 Create the trigger

Console → **Cloud Build** → **Triggers** → **+ Create Trigger**.

- Name: **`den-bot-server-main`**
- Region: **global** (or us-east4 if available — both work)
- Event: **Push to a branch**
- Source: the repo connected in E.1
- Branch (regex): `^main$`
- Configuration:
  - Type: **Cloud Build configuration file (yaml or json)**
  - Location: **Repository**
  - File: `cloudbuild.yaml`
- Service account: leave as **Cloud Build default SA** (`<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`)
- **Create**

### E.3 (Optional) Add an `includedFiles` filter later

If you find doc-only PRs are triggering needless builds, edit the trigger and set **Included files filter** to something like:
```
app/**, requirements.txt, Dockerfile, cloudbuild.yaml
```
Skipping for now per the deployment plan — "every merge to main" is simpler and revisions are cheap.

---

## Phase F — First deploy + smoke test

### F.1 Trigger manually

Console → **Cloud Build** → **Triggers** → `den-bot-server-main` → **Run** (top right).
- Branch: `main`
- **Run trigger**

Watch the build in **Cloud Build** → **History**. Expected duration: 3–6 minutes (first build pulls base layers).

If it fails: read the step logs. Common first-build issues:
- IAM: build hits a "permission denied" in step 3 (deploy) → re-check Phase A.2 grants for the Cloud Build SA.
- Secret not found: deploy fails with `secret not found` → verify the name matches exactly (Phase D).
- Redis init error: container logs show `AsyncRedisSaver.asetup()` failure → confirm `REDIS_URL` points at a Redis 8 database and the modules verified in Phase C.3.

### F.2 Hit /health

Console → **Cloud Run** → click `den-bot-server` → **URL** at the top (something like `https://den-bot-server-xxxxxxxxxx-uk.a.run.app`).

```bash
curl https://<service-url>/health
```

Expected: 200 OK.

### F.3 Smoke from the frontend

Update the frontend's `API_URL` env var (or equivalent) to point at the new Cloud Run URL and redeploy. Then run the same query set from the Qdrant Phase 6 smoke:

1. *"What are the demographics of Five Points?"* — demographics + filtered-scroll path
2. *"What's the weather like in Capitol Hill today?"* — weather tool (filtered scroll)
3. *"When's the next bus from Union Station?"* — RTD arrivals tool
4. *"How do I get to DIA from downtown on public transit?"* — heavy hybrid retrieval

Watch for:
- 500s or stream truncation in browser dev tools
- Cloud Run logs (**Cloud Run** → service → **Logs**) for tracebacks
- LangSmith project `blue-cypher-prod` for trace failures

---

## Operations

### Rolling back to a previous revision
**Cloud Run** → service → **Revisions** tab → previous revision → **Manage Traffic** → 100% to the older revision. Instant; no rebuild needed.

### Updating env vars / config (non-secret)
Edit `cloudbuild.yaml`, push to main, trigger fires. Or for a one-off hotfix: **Cloud Run** → service → **Edit & Deploy New Revision** → adjust → **Deploy**. (The next CI deploy will overwrite manual edits, so prefer editing `cloudbuild.yaml`.)

### Rotating a secret
**Secret Manager** → secret → **+ New Version** → paste new value. Cloud Run picks up `:latest` on the next deploy. To force pickup without a code change, redeploy the same revision via Cloud Build "Run trigger".

### Viewing logs
**Cloud Run** → service → **Logs** tab. Filter by severity. SSE responses appear as one log per response, not per token.

### Cost ballpark (steady state, light traffic)
- Cloud Run: pay-per-request, ~$0–5/mo for hobby traffic
- Redis Cloud Essentials free tier: $0 (30 MB)
- Artifact Registry storage: <$1/mo
- Cloud Build: 120 free build-minutes/day; well under that
- Qdrant Cloud free tier: $0
- **Total**: ~$0–5/mo

### Tearing down the original Memorystore + VPC stack

If you initially provisioned Memorystore + a VPC connector (per an earlier version of this runbook) before settling on Redis Cloud, delete these to stop the ~$45/mo bill:

1. **Memorystore Redis**: Memorystore → Redis → `den-bot-redis` → **Delete**.
2. **Serverless VPC Access Connector**: VPC network → Serverless VPC access → `den-bot-connector` → **Delete**.
   - ⚠️ Deleting the connector alone is not enough — the Cloud Run service still has a *reference* to the connector in its config, and every subsequent deploy will fail with `VPC connector ... does not exist` until that reference is cleared. The current `cloudbuild.yaml` handles this automatically via `--clear-vpc-connector` on the deploy step (idempotent). If deploying outside Cloud Build, pass that same flag to `gcloud run deploy` once. (There is no `--clear-vpc-egress` counterpart; the egress setting is inert without a connector to route through.)
3. **Private services connection** (optional — no ongoing cost, but unused): VPC network → VPC networks → `default` → Private services connection → Connections → delete the Google Cloud Platform connection. Then Allocated IP ranges → delete `google-managed-services-default`.
4. **Disable now-unused APIs** (optional, free): APIs & Services → disable *Memorystore for Redis API*, *Serverless VPC Access API*, *Service Networking API*.
5. **Trim the runtime SA** (optional, hygiene): IAM → Cloud Run runtime SA → remove `roles/vpcaccess.user` if it was granted earlier.

The allocated IP range and connection are free if left in place — only delete them if you want a clean slate.

### Tightening for prod (later)
- Replace `--allow-unauthenticated` with IAP or a shared-secret header check
- Add a token-bucket rate limiter on `/query` (Gemini calls aren't free)
- Upgrade Redis Cloud tier if checkpoint storage exceeds the 30 MB free Essentials cap (each /query writes a few checkpoint keys with a 30-day TTL)
- Tighten `ALLOWED_ORIGINS` (already done — only `bluecypher.ai` + `www.bluecypher.ai` in `cloudbuild.yaml`)
- Move ingest scripts into a one-shot Cloud Run Job for prod re-ingests (instead of running locally against the cloud URL)
