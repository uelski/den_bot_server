"""Qdrant KB collection setup and chunk upsert.

Hybrid collection: dense (gemini-embedding-001, 3072-dim, cosine) +
sparse (BM25 via fastembed). Matches the existing catalog collection
shape so the retrieval-side code (Step G) can use the same fusion
pattern across both collections.

Point IDs are deterministic UUIDs derived from (document_id, child_index)
so re-running ingestion on the same PDF overwrites rather than duplicates
— this is what makes the Pub/Sub at-least-once delivery semantics safe.
"""

import logging
import os
import uuid
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    PointStruct,
    SparseVectorParams,
    VectorParams,
)

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
QDRANT_KB_COLLECTION_NAME = os.getenv(
    "QDRANT_KB_COLLECTION_NAME", "denver_pdf_knowledge_base"
)

# gemini-embedding-001 native output dim
EMBEDDING_DIM = 3072

# Named vector keys used in PointStruct.vector. The retriever (Step G)
# must use the same names; keep them in sync via these constants.
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Fixed UUID namespace so (document_id, child_index) → stable point ID
# across worker restarts and across replicas. The value itself is
# arbitrary; do NOT change it once data is ingested or every point will
# get a new ID on re-ingestion (defeating idempotency).
POINT_NAMESPACE = uuid.UUID("4d8d6f30-7e3f-4f8a-9d2b-1c1a2a3b4c5d")


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def ensure_kb_collection() -> None:
    """Create the hybrid KB collection if it doesn't already exist."""
    client = get_client()
    if client.collection_exists(QDRANT_KB_COLLECTION_NAME):
        return
    client.create_collection(
        collection_name=QDRANT_KB_COLLECTION_NAME,
        vectors_config={
            DENSE_VECTOR_NAME: VectorParams(
                size=EMBEDDING_DIM, distance=Distance.COSINE
            ),
        },
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: SparseVectorParams(),
        },
    )
    logger.info(
        "created qdrant kb collection (hybrid: dense+%s, sparse+bm25): %s",
        EMBEDDING_DIM,
        QDRANT_KB_COLLECTION_NAME,
    )


def deterministic_point_id(document_id: str, child_index: int) -> str:
    """Stable UUID for a given (document_id, child_index) pair."""
    return str(uuid.uuid5(POINT_NAMESPACE, f"{document_id}:{child_index}"))


def upsert_chunks(points: list[PointStruct]) -> None:
    """Batch upsert points to the KB collection."""
    if not points:
        return
    get_client().upsert(collection_name=QDRANT_KB_COLLECTION_NAME, points=points)
    logger.info("upserted %d points to %s", len(points), QDRANT_KB_COLLECTION_NAME)
