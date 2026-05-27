"""worker/main.py — FastAPI Pub/Sub push endpoint for PDF ingestion.

Receives Pub/Sub push notifications when a PDF lands in the GCS bucket
configured by the bucket notification. Parses the envelope, extracts the
GCS object location + custom metadata, then (eventually) hands off to the
ingestion pipeline (parse → chunk → embed → upsert).

This is a separate Cloud Run service from the main API (deployed via
worker/Dockerfile). Same repo, different runtime. See ITERATION_V2.md §
PDF Knowledge Base for the full design.

Ack semantics (matters for production retries + DLQ):
  - 2xx : success → Pub/Sub drops the message
  - 5xx : transient failure → Pub/Sub retries with backoff
  - 4xx : permanent failure → after max attempts, lands in the DLQ topic
"""

import base64
import json
import logging

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="den_bot_worker", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for Cloud Run."""
    return {"status": "ok"}


@app.post("/pubsub/pdf-ingest", status_code=status.HTTP_204_NO_CONTENT)
async def pdf_ingest(request: Request) -> None:
    """Pub/Sub push subscription target. Expected body shape:

        {
          "subscription": "projects/<p>/subscriptions/<s>",
          "message": {
            "messageId": "...",
            "publishTime": "...",
            "attributes": { "eventType": "OBJECT_FINALIZE", ... },
            "data": "<base64-encoded GCS object JSON>"
          }
        }

    The base64 `data` decodes to the full GCS object metadata (set via
    --payload-format=json on the bucket notification). That includes the
    `metadata` dict carrying all our x-goog-meta-* values with the prefix
    stripped.

    Returns 204 on success. Malformed envelopes return 400 so Pub/Sub
    routes them to the DLQ rather than retrying forever.
    """
    envelope = await request.json()

    message = envelope.get("message")
    if not message or "data" not in message:
        logger.warning("malformed envelope: missing message.data")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid pubsub envelope: missing message.data",
        )

    try:
        gcs_event = json.loads(base64.b64decode(message["data"]))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("could not decode message.data: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"could not decode message.data: {exc}",
        )

    bucket = gcs_event.get("bucket")
    object_name = gcs_event.get("name")
    custom_metadata = gcs_event.get("metadata") or {}

    if not bucket or not object_name:
        logger.warning("missing bucket or object name in gcs event")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="gcs event missing bucket or object name",
        )

    logger.info(
        "ingest received: bucket=%s object=%s document_title=%r category=%s",
        bucket,
        object_name,
        custom_metadata.get("document_title", "<missing>"),
        custom_metadata.get("category", "<missing>"),
    )

    # Pipeline hand-off lands in build step B (download → parse → chunk →
    # embed → upsert). Until then, parsing the envelope IS the test.
    return None
