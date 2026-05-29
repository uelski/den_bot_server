"""Top-level ingestion pipeline: bytes → chunks → embedded → upserted.

This is the function the Pub/Sub push handler calls. Each step lives in
its own module for testability; this file just orchestrates and is the
single place where the Qdrant point payload schema is materialized.

Idempotent end-to-end: re-running on the same `document_id` produces
identical point IDs and overwrites in place — that's what makes the
at-least-once Pub/Sub delivery safe.
"""

import logging
from dataclasses import dataclass

from qdrant_client.http.models import PointStruct, SparseVector

from worker.pipeline.chunker import chunk_pages
from worker.pipeline.embedder import embed_dense, embed_sparse
from worker.pipeline.gcs import download_pdf
from worker.pipeline.parser import parse_pdf
from worker.pipeline.qdrant_writer import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    deterministic_point_id,
    ensure_kb_collection,
    upsert_chunks,
)

logger = logging.getLogger(__name__)

SOURCE_COLLECTION = "knowledge_base"
DEFAULT_CATEGORY = "general"


@dataclass
class IngestResult:
    document_id: str
    parents: int
    children: int


def _build_payload(
    *,
    document_id: str,
    document_title: str,
    original_filename: str,
    category: str,
    source_url: str,
    uploaded_at: str,
    child,
    parent,
) -> dict:
    return {
        # Document-level (constant across siblings of this doc)
        "document_id": document_id,
        "document_title": document_title,
        "original_filename": original_filename,
        "category": category,
        "source_url": source_url,
        "source_collection": SOURCE_COLLECTION,
        "uploaded_at": uploaded_at,
        # Child-level
        "child_index": child.child_index,
        "child_text": child.text,
        "child_start_page": child.start_page,
        "child_end_page": child.end_page,
        # Parent-level (denormalized — duplicated across siblings)
        "parent_index": child.parent_index,
        "parent_text": parent.text,
        "parent_start_page": parent.start_page,
        "parent_end_page": parent.end_page,
    }


def process_pdf(
    bucket: str,
    object_name: str,
    custom_metadata: dict,
) -> IngestResult:
    """Run the full ingestion pipeline for one uploaded PDF.

    Steps: download from GCS → parse with pymupdf → split into parent +
    child chunks → embed children with gemini-embedding-001 → upsert to
    Qdrant KB collection with denormalized parent text in each point's
    payload.

    Custom metadata keys come from the GCS object's `x-goog-meta-*`
    headers (prefix stripped). Missing optional fields fall back to
    sensible defaults; `document_id` defaults to the GCS object path.
    """
    document_id = custom_metadata.get("document_id") or object_name
    document_title = custom_metadata.get("document_title", "")
    original_filename = custom_metadata.get("original_filename", "")
    category = custom_metadata.get("category", DEFAULT_CATEGORY)
    source_url = custom_metadata.get("source_url", "")
    uploaded_at = custom_metadata.get("uploaded_at", "")

    pdf_bytes = download_pdf(bucket, object_name)
    pages = parse_pdf(pdf_bytes)
    parents, children = chunk_pages(pages)

    if not children:
        logger.warning(
            "no children produced for document_id=%s (empty pdf or all-whitespace?)",
            document_id,
        )
        return IngestResult(document_id=document_id, parents=0, children=0)

    child_texts = [c.text for c in children]
    dense_vectors = embed_dense(child_texts)
    sparse_vectors = embed_sparse(child_texts)

    ensure_kb_collection()
    parent_by_index = {p.parent_index: p for p in parents}

    points = [
        PointStruct(
            id=deterministic_point_id(document_id, child.child_index),
            vector={
                DENSE_VECTOR_NAME: dense_v,
                SPARSE_VECTOR_NAME: SparseVector(
                    indices=sparse_indices, values=sparse_values
                ),
            },
            payload=_build_payload(
                document_id=document_id,
                document_title=document_title,
                original_filename=original_filename,
                category=category,
                source_url=source_url,
                uploaded_at=uploaded_at,
                child=child,
                parent=parent_by_index[child.parent_index],
            ),
        )
        for child, dense_v, (sparse_indices, sparse_values) in zip(
            children, dense_vectors, sparse_vectors
        )
    ]
    upsert_chunks(points)

    logger.info(
        "ingest complete: document_id=%s parents=%d children=%d",
        document_id,
        len(parents),
        len(children),
    )
    return IngestResult(
        document_id=document_id,
        parents=len(parents),
        children=len(children),
    )
