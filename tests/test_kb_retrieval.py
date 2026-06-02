"""Unit tests for app/retrieval/kb.py — payload→Document mapping + empty-collection guard."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.retrieval import kb as kb_module


class TestPointToDocument:
    def test_maps_child_text_to_page_content(self):
        payload = {
            "child_text": "the matched chunk",
            "parent_text": "the bigger parent",
            "document_title": "Ordinances",
            "parent_start_page": 3,
            "parent_end_page": 5,
            "source_collection": "knowledge_base",
        }
        doc = kb_module._point_to_document(payload)
        assert doc.page_content == "the matched chunk"
        assert doc.metadata["parent_text"] == "the bigger parent"
        assert doc.metadata["document_title"] == "Ordinances"

    def test_backfills_provenance_when_missing(self):
        doc = kb_module._point_to_document({"child_text": "x"})
        assert doc.metadata["source_collection"] == "knowledge_base"


class TestSearchKb:
    def test_returns_empty_when_collection_absent(self):
        kb_module._get_client.cache_clear()
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = False
        with patch.object(kb_module, "_get_client", return_value=mock_client):
            assert kb_module.search_kb("anything") == []
        mock_client.query_points.assert_not_called()

    def test_maps_query_points_into_documents(self):
        kb_module._get_client.cache_clear()
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        mock_client.query_points.return_value = SimpleNamespace(
            points=[
                SimpleNamespace(payload={"child_text": "a", "document_id": "d1"}),
                SimpleNamespace(payload={"child_text": "b", "document_id": "d1"}),
            ]
        )
        sparse = SimpleNamespace(
            indices=SimpleNamespace(tolist=lambda: [1]),
            values=SimpleNamespace(tolist=lambda: [0.5]),
        )
        mock_dense = MagicMock()
        mock_dense.embed_query.return_value = [0.1, 0.2]
        mock_sparse = MagicMock()
        mock_sparse.embed.return_value = iter([sparse])

        with patch.object(kb_module, "_get_client", return_value=mock_client), \
            patch.object(kb_module, "_get_dense_embedder", return_value=mock_dense), \
            patch.object(kb_module, "_get_sparse_embedder", return_value=mock_sparse):
            docs = kb_module.search_kb("zoning", k=10)

        assert [d.page_content for d in docs] == ["a", "b"]
        assert all(d.metadata["source_collection"] == "knowledge_base" for d in docs)
