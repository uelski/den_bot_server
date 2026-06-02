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
  3. Drop candidates below RERANK_SCORE_THRESHOLD. Retrieval always returns a
     full candidate pool per collection, so without this a single-domain query
     (e.g. a budget-PDF question) still gets its top-5 padded with weakly
     related catalog services. The score cut keeps only genuinely relevant
     docs — which is what both the generator AND the sources/map_viewers SSE
     events are built from, so it fixes irrelevant context and irrelevant
     citations in one step.
  4. Take the top-5 of what survives.
  5. Expand surviving KB hits from child_text → parent_text, so the generator
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

# Minimum Cohere relevance_score (0–1) for a doc to be kept. Cohere scores are
# relative, not calibrated probabilities, so this is empirical — watch the
# per-query scores logged below and tune. Tunable in prod via env without a
# code change. Only applied when Cohere actually ran (fail-open keeps all).
RERANK_SCORE_THRESHOLD = float(os.getenv("RERANK_SCORE_THRESHOLD", "0.3"))


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
    """Return `docs` reordered by Cohere relevance, stamping each doc's
    `metadata['rerank_score']`. Fail-open to input order (no scores stamped)
    if Cohere is unavailable or errors."""
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
        ordered: list[Document] = []
        for r in result.results:
            doc = docs[r.index]
            doc.metadata["rerank_score"] = r.relevance_score
            ordered.append(doc)
        return ordered
    except Exception:
        logger.exception("cohere rerank failed; falling back to fused order")
        return docs


def _filter_by_score(docs: list[Document]) -> list[Document]:
    """Drop docs the reranker scored below the relevance threshold.

    Docs with no `rerank_score` (the fail-open path, when Cohere didn't run)
    always pass — we can't threshold what we never scored, so the old
    take-top-N behavior is preserved when reranking is unavailable.
    """
    kept = []
    for d in docs:
        score = d.metadata.get("rerank_score")
        if score is None or score >= RERANK_SCORE_THRESHOLD:
            kept.append(d)
    return kept


def reranker(state: AgentState) -> dict:
    """Rerank + dedupe + score-filter + parent-expand the merged candidates."""
    docs = state.get("retrieved_docs") or []
    if not docs:
        return {"retrieved_docs": []}

    query = state.get("search_query") or state["query"]

    ranked = _rerank_order(query, docs)
    deduped = _dedupe_kb_siblings(ranked)
    relevant = _filter_by_score(deduped)
    top = relevant[:TOP_N]
    expanded = [_expand_to_parent(d) for d in top]

    logger.info(
        "reranker: %d candidates -> %d deduped -> %d >= %.2f -> top %d | scores=%s",
        len(docs), len(deduped), len(relevant), RERANK_SCORE_THRESHOLD, len(expanded),
        [
            round(d.metadata["rerank_score"], 3)
            for d in top
            if d.metadata.get("rerank_score") is not None
        ],
    )
    return {"retrieved_docs": expanded}
