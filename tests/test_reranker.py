"""Unit tests for the reranker node — dedupe, parent-expansion, fail-open."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.graph.nodes import reranker as reranker_module


def _fake_rerank_result(order):
    """Build a Cohere-like rerank response that reorders by the given indices."""
    return SimpleNamespace(results=[SimpleNamespace(index=i) for i in order])


class TestDedupeKbSiblings:
    def test_collapses_children_of_same_parent(self, kb_doc_factory, catalog_doc):
        a = kb_doc_factory(document_id="d1", parent_index=0, child_index=0)
        b = kb_doc_factory(document_id="d1", parent_index=0, child_index=1)
        c = kb_doc_factory(document_id="d1", parent_index=1, child_index=2)
        out = reranker_module._dedupe_kb_siblings([a, b, c, catalog_doc])
        # b is dropped (same document_id+parent_index as a); order preserved.
        assert out == [a, c, catalog_doc]

    def test_keeps_first_occurrence_highest_ranked(self, kb_doc_factory):
        first = kb_doc_factory(document_id="d1", parent_index=0, child_index=5)
        second = kb_doc_factory(document_id="d1", parent_index=0, child_index=6)
        out = reranker_module._dedupe_kb_siblings([first, second])
        assert out == [first]

    def test_catalog_docs_never_deduped(self, catalog_doc, make_doc):
        other = make_doc(service_name="Libraries", source_collection="catalog")
        out = reranker_module._dedupe_kb_siblings([catalog_doc, other])
        assert out == [catalog_doc, other]


class TestExpandToParent:
    def test_kb_doc_content_becomes_parent_text(self, kb_doc_factory):
        doc = kb_doc_factory(
            document_id="d1", child_text="just the child", parent_text="WHOLE PARENT"
        )
        expanded = reranker_module._expand_to_parent(doc)
        assert expanded.page_content == "WHOLE PARENT"
        # original child preserved in metadata
        assert expanded.metadata["child_text"] == "just the child"

    def test_catalog_doc_passes_through(self, catalog_doc):
        assert reranker_module._expand_to_parent(catalog_doc) is catalog_doc


class TestRerankerNode:
    def test_empty_docs_short_circuits(self):
        assert reranker_module.reranker({"retrieved_docs": []}) == {"retrieved_docs": []}

    def test_reranks_dedupes_and_expands(self, kb_doc_factory, catalog_doc):
        reranker_module._get_cohere_client.cache_clear()
        c0 = kb_doc_factory(document_id="d1", parent_index=0, child_index=0)
        c1 = kb_doc_factory(document_id="d1", parent_index=0, child_index=1)
        docs = [catalog_doc, c0, c1]

        mock_client = MagicMock()
        # Cohere ranks: c0 (idx1), c1 (idx2), catalog (idx0)
        mock_client.rerank.return_value = _fake_rerank_result([1, 2, 0])

        with patch.object(
            reranker_module, "_get_cohere_client", return_value=mock_client
        ):
            result = reranker_module.reranker(
                {"query": "noise ordinance", "retrieved_docs": docs}
            )

        out = result["retrieved_docs"]
        # c0 kept, c1 (sibling) dropped, catalog follows → 2 docs.
        assert len(out) == 2
        assert out[0].page_content == c0.metadata["parent_text"]  # expanded
        assert out[1] is catalog_doc

    def test_fails_open_to_fused_order_without_key(self, kb_doc_factory, catalog_doc):
        reranker_module._get_cohere_client.cache_clear()
        kb = kb_doc_factory(document_id="d1")
        with patch.object(reranker_module, "_get_cohere_client", return_value=None):
            result = reranker_module.reranker(
                {"query": "q", "retrieved_docs": [catalog_doc, kb]}
            )
        # No rerank, but dedupe + expand + truncate still run; order preserved.
        out = result["retrieved_docs"]
        assert out[0] is catalog_doc
        assert out[1].page_content == kb.metadata["parent_text"]

    def test_fails_open_when_cohere_raises(self, kb_doc_factory, catalog_doc):
        reranker_module._get_cohere_client.cache_clear()
        mock_client = MagicMock()
        mock_client.rerank.side_effect = RuntimeError("cohere down")
        with patch.object(
            reranker_module, "_get_cohere_client", return_value=mock_client
        ):
            result = reranker_module.reranker(
                {"query": "q", "retrieved_docs": [catalog_doc]}
            )
        assert result["retrieved_docs"] == [catalog_doc]

    def test_truncates_to_top_n(self, kb_doc_factory):
        reranker_module._get_cohere_client.cache_clear()
        # 7 distinct parents → after dedupe still 7 → should trim to TOP_N (5).
        docs = [kb_doc_factory(document_id="d1", parent_index=i) for i in range(7)]
        with patch.object(reranker_module, "_get_cohere_client", return_value=None):
            result = reranker_module.reranker({"query": "q", "retrieved_docs": docs})
        assert len(result["retrieved_docs"]) == reranker_module.TOP_N
