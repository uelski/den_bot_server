# Deployment Runbook — den_bot_server

Console-driven setup for project **`blue-cypher`** in region **`us-east4`** (matches the Qdrant Cloud cluster).

## Architecture

```
GitHub (main push)
   └── Cloud Build trigger ── cloudbuild.yaml
            └── builds image → Artifact Registry (us-east4)
                       └── deploys → Cloud Run service "den-bot-server"
                                          │
                                          ├── public: Gemini, Qdrant Cloud, RTD feeds, Tavily, Resend
                                          └── private (via VPC connector): Memorystore Redis
                                          
                                  Secret Manager: GEMINI_API_KEY, QDRANT_*, REDIS_URL, RESEND_*, LANGCHAIN_*, TAVILY_API_KEY
```

| Resource | Name | Region |
|---|---|---|
| Artifact Registry repo | `den-bot` | us-east4 |
| Cloud Run service | `den-bot-server` | us-east4 |
| VPC connector | `den-bot-connector` | us-east4 |
| Memorystore Redis | `den-bot-redis` (Basic, 1 GB) | us-east4 |
| Build trigger | `den-bot-server-main` | global |

---

## Phase A — Enable APIs + IAM

### A.1 Enable APIs
Console → **APIs & Services** → **Enabled APIs & Services** → **+ Enable APIs and Services**. Search and enable:

- Cloud Run Admin API
- Cloud Build API
- Artifact Registry API
- Secret Manager API
- Memorystore for Redis API
- Serverless VPC Access API
- Service Networking API
- Compute Engine API

Wait until all show "Enabled".

### A.2 Grant Cloud Build SA the deploy permissions

The Cloud Build default service account is `<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com`. Find the project number under **IAM & Admin** → **Settings**.

Console → **IAM & Admin** → **IAM**. If the Cloud Build SA isn't listed, click **Grant Access** and add the principal. Then assign these roles to it:

- **Cloud Run Admin** (`roles/run.admin`) — deploy revisions
- **Service Account User** (`roles/iam.serviceAccountUser`) — needed to "act as" the Cloud Run runtime SA during deploy
- **Artifact Registry Writer** (`roles/artifactregistry.writer`) — push images
- **Secret Manager Secret Accessor** (`roles/secretmanager.secretAccessor`) — read secrets during validation (Cloud Run does this on the runtime SA, but Cloud Build inspects them at deploy)
- **Logs Writer** (`roles/logging.logWriter`) — required because cloudbuild.yaml uses `options.logging: CLOUD_LOGGING_ONLY`

### A.3 Grant Cloud Run runtime SA the secret + VPC permissions

The Cloud Run **runtime** SA defaults to the Compute Engine default SA: `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`. Grant it:

- **Secret Manager Secret Accessor** (`roles/secretmanager.secretAccessor`) — read secrets at container startup
- **Serverless VPC Access User** (`roles/vpcaccess.user`) — route through the VPC connector

You can grant these on individual secrets in Phase D for tighter scope, but a project-level grant is simpler. Choose either.

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

## Phase C — VPC + Memorystore Redis

Memorystore Basic instances only expose **private IPs**, so Cloud Run must reach them via a Serverless VPC Access Connector.

### C.1 Reserve a private services access range (one-time per VPC)

Console → **VPC network** → **VPC networks** → click **`default`**.

- Tab: **Private services connection** → **Allocated IP ranges for services**
- **+ Allocate IP Range**
  - Name: **`google-managed-services-default`**
  - Description: "Range for Memorystore / private services"
  - **Automatic allocation**, **/16**
  - Click **Allocate**

Then tab: **Private services connection** → **Connections** → **+ Create Connection**.
- Service producer: **Google Cloud Platform**
- Allocated ranges: select **`google-managed-services-default`**
- **Connect**

This takes 1–2 minutes.

### C.2 Create Serverless VPC Access Connector

Console → **VPC network** → **Serverless VPC access** → **+ Create Connector**.

- Name: **`den-bot-connector`**
- Region: **us-east4**
- Network: **`default`**
- Subnet: **Custom IP range** → **`10.8.0.0/28`** (any unused /28 in the VPC; verify in the **VPC networks** subnet list)
- Minimum instances: **2** (default)
- Maximum instances: **3** (default; can raise later)
- Instance type: **f1-micro** (cheapest; fine for our traffic)
- Click **Create**

Wait until status is "Ready" (~2 minutes).

### C.3 Create Memorystore Redis instance

Console → **Memorystore** → **Redis** → **+ Create Instance**.

- Instance ID: **`den-bot-redis`**
- Tier: **Basic** (no replication)
- Region: **us-east4**, Zone: **Any (let Google choose)**
- Capacity: **1 GB**
- Version: **Redis 7.x** (latest available)
- Network: **`default`**
- Connection mode: **Private service access** (uses the connection we made in C.1)
- AUTH: **Disabled** (Memorystore Basic in a private VPC; no internet exposure). If you'd rather have AUTH, enable it and the REDIS_URL becomes `redis://default:<AUTH_STRING>@<IP>:6379`.
- Click **Create**

Provisioning takes ~5 minutes. Once ready, **click into the instance** and capture the **Primary endpoint** (something like `10.x.x.3:6379`). This becomes the value of the `REDIS_URL` secret in Phase D: 10.118.0.3

```
redis://10.118.0.3:6379
```

> **Note**: `langgraph-checkpoint-redis` uses RediSearch under the hood, which Memorystore Redis 7.x supports natively. If you see `FT.*` command errors at runtime, double-check the version.

---

## Phase D — Secret Manager

Console → **Security** → **Secret Manager** → **+ Create Secret**.

Create one secret per env var below. For each: name (exactly as listed), paste the value, **Add Secret**. Region: **automatic / global replication** (simplest).

| Secret name | Value source |
|---|---|
| `GEMINI_API_KEY` | from your local `.env` |
| `QDRANT_URL` | `https://a034457f-75ff-486d-b27e-225898eecacb.us-east4-0.gcp.cloud.qdrant.io` (from `.env.production`) |
| `QDRANT_API_KEY` | from `.env.production` |
| `REDIS_URL` | `redis://10.x.x.3:6379` (from Phase C.3) |
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
- VPC connector not ready: deploy fails with `vpc connector not found` → verify Phase C.2 status is "Ready".
- Secret not found: deploy fails with `secret not found` → verify the name matches exactly (Phase D).

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
- Memorystore Basic 1 GB: ~$35/mo
- VPC Connector (2 × f1-micro): ~$10/mo
- Artifact Registry storage: <$1/mo
- Cloud Build: 120 free build-minutes/day; well under that
- Qdrant Cloud free tier: $0
- **Total**: ~$45–55/mo

### Tightening for prod (later)
- Replace `--allow-unauthenticated` with IAP or a shared-secret header check
- Add a token-bucket rate limiter on `/query` (Gemini calls aren't free)
- Switch Memorystore to Standard HA if conversation continuity becomes important
- Tighten `ALLOWED_ORIGINS` (already done — only `bluecypher.ai` + `www.bluecypher.ai` in `cloudbuild.yaml`)
- Move ingest scripts into a one-shot Cloud Run Job for prod re-ingests (instead of running locally against the cloud URL)
