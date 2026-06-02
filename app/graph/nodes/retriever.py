"""retriever node — hybrid Qdrant vector search across both collections.

Fans out across the catalog collection (GIS services) and the PDF knowledge
base in parallel, then returns their union for the reranker to merge. A single
query can legitimately want both — e.g. "Denver zoning" wants the GIS zoning
layer AND the zoning ordinance PDF. Hybrid scores aren't comparable across
collections, so this node does NOT rank the union; the reranker node does
(see app/graph/nodes/reranker.py).
"""

import asyncio
import os
from functools import lru_cache

from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

from app.graph.state import AgentState
from app.retrieval.kb import search_kb

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

# Candidate pool per collection BEFORE rerank. Larger than the final top-5 so
# the cross-encoder has room to reorder; the reranker trims to TOP_N.
CANDIDATE_K = 20


@lru_cache(maxsize=1)
def _get_vector_store() -> QdrantVectorStore:
    """Singleton catalog Qdrant store — avoids re-creating connections per call."""
    dense = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    sparse = FastEmbedSparse(model_name="Qdrant/bm25")
    return QdrantVectorStore.from_existing_collection(
        embedding=dense,
        sparse_embedding=sparse,
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
    )


def _tag_catalog(docs: list[Document]) -> list[Document]:
    """Stamp catalog provenance so the reranker/generator/sources can branch."""
    for d in docs:
        d.metadata.setdefault("source_collection", "catalog")
    return docs


async def _search_catalog(query: str) -> list[Document]:
    store = _get_vector_store()
    docs = await store.asimilarity_search(query, k=CANDIDATE_K)
    return _tag_catalog(docs)


async def retriever(state: AgentState) -> dict:
    """Run hybrid search against both collections concurrently; return the union.

    Prefers `search_query` (history-aware standalone form set by the condenser,
    or rewriter on retry) and falls back to the literal `query` for safety.
    The merged set is unranked across collections — the reranker node orders it.
    """
    search_q = state.get("search_query") or state["query"]

    catalog_docs, kb_docs = await asyncio.gather(
        _search_catalog(search_q),
        asyncio.to_thread(search_kb, search_q, CANDIDATE_K),
    )
    return {"retrieved_docs": catalog_docs + kb_docs}
