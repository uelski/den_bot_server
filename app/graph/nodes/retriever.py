"""retriever node — hybrid Qdrant vector search."""

import os
from functools import lru_cache

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

from app.graph.state import AgentState

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")
TOP_K = 5


@lru_cache(maxsize=1)
def _get_vector_store() -> QdrantVectorStore:
    """Singleton Qdrant store — avoids re-creating connections per call."""
    dense = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    sparse = FastEmbedSparse(model_name="Qdrant/bm25")
    return QdrantVectorStore.from_existing_collection(
        embedding=dense,
        sparse_embedding=sparse,
        url=QDRANT_URL,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
    )


def retriever(state: AgentState) -> dict:
    """Run hybrid search against Qdrant; return top-k documents."""
    store = _get_vector_store()
    docs = store.similarity_search(state["query"], k=TOP_K)
    return {"retrieved_docs": docs}
