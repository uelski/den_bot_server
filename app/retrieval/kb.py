"""Hybrid retrieval against the PDF knowledge-base collection.

The worker (`worker/pipeline/qdrant_writer.py`) creates
`denver_pdf_knowledge_base` with named vectors `dense`/`sparse` and a FLAT
payload (`child_text`, `parent_text`, `document_title`, page ranges, …) —
deliberately NOT the LangChain `page_content` + nested `metadata` shape the
catalog collection uses. So we can't point the catalog's `QdrantVectorStore`
at it; instead we query `qdrant_client` directly with server-side RRF fusion
over the two named vectors, then map each child hit into a LangChain
`Document` the rest of the graph already understands.

Provenance: every Document carries `source_collection="knowledge_base"` so
the reranker, generator, and sources builder can branch on it. `page_content`
is the **child_text** (the ~300-token chunk that actually matched) — the
reranker scores against that, then expands the survivors to `parent_text`
for the generator (see `app/graph/nodes/reranker.py`).
"""

import logging
import os
from functools import lru_cache

from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.http.models import Fusion, FusionQuery, Prefetch, SparseVector

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
KB_COLLECTION_NAME = os.getenv("QDRANT_KB_COLLECTION_NAME", "denver_pdf_knowledge_base")

# Must match the names the worker upserts under
# (worker/pipeline/qdrant_writer.py DENSE/SPARSE_VECTOR_NAME).
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Same embedding models as the catalog so the two vector spaces align.
DENSE_MODEL = "models/gemini-embedding-001"
SPARSE_MODEL = "Qdrant/bm25"


@lru_cache(maxsize=1)
def _get_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


@lru_cache(maxsize=1)
def _get_dense_embedder() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(model=DENSE_MODEL)


@lru_cache(maxsize=1)
def _get_sparse_embedder():
    """BM25 sparse embedder via fastembed. Model files download on first use."""
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=SPARSE_MODEL)


def _point_to_document(payload: dict) -> Document:
    """Map a KB point's flat payload into a LangChain Document.

    page_content is the child_text (what matched + what the reranker scores).
    Everything else, including parent_text for later expansion, rides in
    metadata.
    """
    metadata = dict(payload)
    # Guarantee the provenance tag even if an older point predates it.
    metadata.setdefault("source_collection", "knowledge_base")
    return Document(
        page_content=payload.get("child_text", ""),
        metadata=metadata,
    )


def search_kb(query: str, k: int = 20) -> list[Document]:
    """Hybrid (dense + sparse, RRF-fused) search over the KB collection.

    Returns up to `k` child-chunk Documents tagged
    `source_collection="knowledge_base"`. Returns `[]` (not an error) when the
    collection doesn't exist yet — i.e. before any PDF has been ingested — so
    the query path keeps working on a fresh deployment.

    Synchronous on purpose: the retriever node calls this via
    `asyncio.to_thread` so it runs concurrently with the catalog search
    without blocking the event loop.
    """
    client = _get_client()

    if not client.collection_exists(KB_COLLECTION_NAME):
        logger.info("kb collection %s not present yet; skipping", KB_COLLECTION_NAME)
        return []

    dense_vec = _get_dense_embedder().embed_query(query)
    sparse = next(iter(_get_sparse_embedder().embed([query])))
    sparse_vec = SparseVector(
        indices=sparse.indices.tolist(), values=sparse.values.tolist()
    )

    response = client.query_points(
        collection_name=KB_COLLECTION_NAME,
        prefetch=[
            Prefetch(query=dense_vec, using=DENSE_VECTOR_NAME, limit=k),
            Prefetch(query=sparse_vec, using=SPARSE_VECTOR_NAME, limit=k),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=k,
        with_payload=True,
    )

    docs = [_point_to_document(p.payload or {}) for p in response.points]
    logger.info("kb search returned %d child hits (k=%d)", len(docs), k)
    return docs
