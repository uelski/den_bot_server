# Deployment Guide — den_bot_server

## Architecture
- **API**: GCP Cloud Run
- **Vector DB**: Qdrant Cloud
- **LLM/Embeddings**: Google Gemini API

## Prerequisites
- GCP project with billing enabled
- `gcloud` CLI installed and authenticated
- Qdrant Cloud account with a cluster provisioned
- Gemini API key

## 1. Qdrant Cloud Setup

1. Create a free-tier cluster at https://cloud.qdrant.io
2. Note the cluster URL and API key
3. Run ingest against the cloud instance:
   ```bash
   QDRANT_URL=https://<your-cluster>.cloud.qdrant.io:6333 \
   QDRANT_API_KEY=<your-key> \
   python scripts/ingest.py
   ```

## 2. GCP Setup

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

Enable required APIs:
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
```

### Store secrets (recommended over env vars)
```bash
echo -n "<your-gemini-key>" | gcloud secrets create GEMINI_API_KEY --data-file=-
echo -n "<your-qdrant-key>" | gcloud secrets create QDRANT_API_KEY --data-file=-
```

## 3. Build & Push Image

Using Artifact Registry (preferred):
```bash
gcloud artifacts repositories create den-bot --repository-format=docker --location=us-central1

gcloud builds submit --tag us-central1-docker.pkg.dev/<YOUR_PROJECT_ID>/den-bot/den-bot-server
```

## 4. Deploy to Cloud Run

```bash
gcloud run deploy den-bot-server \
  --image us-central1-docker.pkg.dev/<YOUR_PROJECT_ID>/den-bot/den-bot-server \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "QDRANT_URL=https://<your-cluster>.cloud.qdrant.io:6333,QDRANT_COLLECTION_NAME=denver_gis_catalog,ALLOWED_ORIGINS=https://<your-frontend-domain>" \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,QDRANT_API_KEY=QDRANT_API_KEY:latest"
```

## 5. Verify

```bash
SERVICE_URL=$(gcloud run services describe den-bot-server --region us-central1 --format='value(status.url)')
curl $SERVICE_URL/health
```

## Notes
- Remove `--allow-unauthenticated` if fronting with IAM auth
- Update `ALLOWED_ORIGINS` to match your production frontend domain(s)
- The Dockerfile is already configured (Python 3.12, port 8080, uvicorn)
- For redeployments, re-run steps 3 and 4
