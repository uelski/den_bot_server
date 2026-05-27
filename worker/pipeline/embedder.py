"""Dense + sparse embeddings for hybrid retrieval.

Dense: Google `gemini-embedding-001` (3072-dim) — same model the main API
uses for query embedding, so the vector spaces align.

Sparse: `Qdrant/bm25` via fastembed — captures exact-term and rare-term
matching that dense embeddings miss. Particularly load-bearing for the
PDF Knowledge Base because legal/regulatory text relies heavily on
specific section references, statutory phrases, and named entities that
bi-encoder dense models tend to fuzz.

Hybrid retrieval (dense + sparse) matches the existing catalog
collection pattern — same shape on read and write.
"""

import logging
import os
from functools import lru_cache

from fastembed import SparseTextEmbedding
from langchain_google_genai import GoogleGenerativeAIEmbeddings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001")
SPARSE_EMBEDDING_MODEL = os.getenv("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25")


@lru_cache(maxsize=1)
def get_dense_embedder() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)


@lru_cache(maxsize=1)
def get_sparse_embedder() -> SparseTextEmbedding:
    """BM25 sparse embedder. Model files download on first use (~few MB)."""
    return SparseTextEmbedding(model_name=SPARSE_EMBEDDING_MODEL)


def embed_dense(texts: list[str]) -> list[list[float]]:
    """Dense embeddings via gemini-embedding-001. Returns vectors in order."""
    if not texts:
        return []
    vectors = get_dense_embedder().embed_documents(texts)
    logger.info(
        "embedded %d texts dense (dim=%d)",
        len(vectors),
        len(vectors[0]) if vectors else 0,
    )
    return vectors


def embed_sparse(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Sparse BM25 embeddings. Returns (indices, values) per text — same
    order as the input list, ready to drop into qdrant_client.SparseVector.
    """
    if not texts:
        return []
    result: list[tuple[list[int], list[float]]] = []
    for sparse in get_sparse_embedder().embed(texts):
        result.append((sparse.indices.tolist(), sparse.values.tolist()))
    logger.info("embedded %d texts sparse (bm25)", len(result))
    return result
