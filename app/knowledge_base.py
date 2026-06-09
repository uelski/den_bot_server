"""Public read API for the PDF knowledge base.

Two unauthenticated endpoints backing the frontend 'About' page:
  GET /knowledge-base/documents            — list distinct ingested documents
  GET /knowledge-base/documents/download   — short-lived signed GET URL for one

Public on purpose: the About page and civic-document downloads are not
sensitive. The document list's source of truth is Qdrant (what's actually
queryable), not the GCS bucket — raw PDFs are kept so downloads work, but the
bucket is storage, not the registry. See ITERATION_V2.md § Upload metadata
('What we give up by skipping a registry').
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Query, status

from app.gcs_signing import generate_download_url
from app.retrieval.kb import list_documents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])

GCS_UPLOAD_BUCKET_ENV = "GCS_UPLOAD_BUCKET"
# document_id IS the GCS object path; only sign objects under the upload prefix
# so this endpoint can't be coerced into signing arbitrary bucket objects.
OBJECT_PREFIX = "pdfs/"


def _bucket_name() -> str:
    name = (os.getenv(GCS_UPLOAD_BUCKET_ENV) or "").strip().strip('"').strip("'")
    if not name:
        logger.error("knowledge-base endpoint hit but GCS_UPLOAD_BUCKET not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge base storage not configured",
        )
    return name


@router.get("/documents")
async def get_documents() -> dict:
    """List distinct documents in the knowledge base (deduped by document_id).

    Each entry: document_id, document_title, category, source_url (nullable —
    the 'View on denvergov.org' link), original_filename, uploaded_at,
    num_chunks, page_count. Newest first. Returns {"documents": []} before any
    PDF is ingested.
    """
    return {"documents": list_documents()}


@router.get("/documents/download")
async def get_document_download_url(
    document_id: str = Query(..., min_length=1),
) -> dict:
    """Issue a short-lived signed GET URL for the raw PDF behind a KB document.

    `document_id` is the GCS object path (as returned by /documents). The
    frontend opens the returned `download_url` directly (top-level GET → no
    CORS needed). Returns 400 for a path outside the upload prefix, 404 if the
    object no longer exists.
    """
    if not document_id.startswith(OBJECT_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid document_id",
        )
    bucket = _bucket_name()
    try:
        url = generate_download_url(bucket, document_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        )
    except Exception as exc:
        logger.exception("failed to sign download url for %s", document_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to generate download url: {exc}",
        )
    return {"document_id": document_id, "download_url": url}
