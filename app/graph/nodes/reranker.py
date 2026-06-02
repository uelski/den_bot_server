"""reranker node — the load-bearing merge step for mixed-provenance retrieval.

The retriever node fans out across two collections (catalog + PDF KB) and
returns their union, unordered relative to each other (hybrid scores aren't
comparable across collections). This node reorders that mixed list with a
cross-encoder so the best chunks — regardless of source — float to the top.

Pipeline (see ITERATION_V2.md § Retrieval changes + Parsing/chunking):
  1. Cohere rerank-english-v3.0 scores every candidate against the query,
     uniformly over page_content (child_text for KB, chunk text for catalog).
  2. Collapse KB siblings: group by (document_id, parent_index), keep the
     highest-ranked child per parent — dedupe AFTER rerank so the reranker
     picks which part of a parent best matched.
  3. Take the top-5 of the collapsed, reranked list.
  4. Expand surviving KB hits from child_text → parent_text, so the generator
     reasons over the full ~1500-token parent (and cites the parent page
     range, not the child page).

Fail-open: if the Cohere call errors or no API key is configured, fall back
to the retriever's order (just truncated to TOP_N) rather than breaking the
graph. A reranker outage degrades quality, it doesn't take down /query.
"""

import logging
import os
from functools import lru_cache

from langchain_core.documents import Document

from app.graph.state import AgentState

logger = logging.getLogger(__name__)

RERANK_MODEL = os.getenv("COHERE_RERANK_MODEL", "rerank-english-v3.0")
TOP_N = 5


@lru_cache(maxsize=1)
def _get_cohere_client():
    """Cohere client, or None if no API key is configured (fail-open)."""
    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        return None
    import cohere

    return cohere.Client(api_key)


def _is_kb(doc: Document) -> bool:
    return doc.metadata.get("source_collection") == "knowledge_base"


def _expand_to_parent(doc: Document) -> Document:
    """Replace a KB child Document's content with its denormalized parent_text.

    Non-KB docs pass through unchanged. The child_text is preserved in
    metadata so nothing is lost.
    """
    if not _is_kb(doc):
        return doc
    parent_text = doc.metadata.get("parent_text")
    if not parent_text:
        return doc
    return Document(page_content=parent_text, metadata=doc.metadata)


def _dedupe_kb_siblings(docs: list[Document]) -> list[Document]:
    """Collapse KB children sharing a parent, keeping the first (highest-ranked).

    Assumes `docs` is already in ranked order. Catalog docs and KB docs with
    no parent key pass through untouched; order is otherwise preserved.
    """
    seen_parents: set[tuple] = set()
    out: list[Document] = []
    for doc in docs:
        if _is_kb(doc):
            key = (doc.metadata.get("document_id"), doc.metadata.get("parent_index"))
            if key in seen_parents:
                continue
            seen_parents.add(key)
        out.append(doc)
    return out


def _rerank_order(query: str, docs: list[Document]) -> list[Document]:
    """Return `docs` reordered by Cohere relevance. Fail-open to input order."""
    client = _get_cohere_client()
    if client is None:
        logger.warning("COHERE_API_KEY not set; skipping rerank (using fused order)")
        return docs
    try:
        result = client.rerank(
            model=RERANK_MODEL,
            query=query,
            documents=[d.page_content for d in docs],
            top_n=len(docs),
        )
        return [docs[r.index] for r in result.results]
    except Exception:
        logger.exception("cohere rerank failed; falling back to fused order")
        return docs


def reranker(state: AgentState) -> dict:
    """Rerank + dedupe + parent-expand the merged candidate set to top-5."""
    docs = state.get("retrieved_docs") or []
    if not docs:
        return {"retrieved_docs": []}

    query = state.get("search_query") or state["query"]

    ranked = _rerank_order(query, docs)
    deduped = _dedupe_kb_siblings(ranked)
    top = deduped[:TOP_N]
    expanded = [_expand_to_parent(d) for d in top]

    logger.info(
        "reranker: %d candidates -> %d after dedupe -> top %d",
        len(docs), len(deduped), len(expanded),
    )
    return {"retrieved_docs": expanded}
