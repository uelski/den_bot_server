"""Unit tests for app/retrieval/kb.py list_documents() — dedup + aggregation."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.retrieval import kb as kb_module


def _pt(**payload):
    return SimpleNamespace(payload=payload)


def _mock_client_with_pages(*pages):
    """Mock a Qdrant client whose .scroll() yields the given pages in order.

    Each page is a list of points; the last page returns offset None.
    """
    client = MagicMock()
    client.collection_exists.return_value = True
    calls = [(list(page), (None if i == len(pages) - 1 else i + 1)) for i, page in enumerate(pages)]
    client.scroll.side_effect = calls
    return client


class TestListDocuments:
    def test_empty_when_collection_absent(self):
        kb_module._get_client.cache_clear()
        client = MagicMock()
        client.collection_exists.return_value = False
        with patch.object(kb_module, "_get_client", return_value=client):
            assert kb_module.list_documents() == []

    def test_dedupes_by_document_id_and_counts_chunks(self):
        kb_module._get_client.cache_clear()
        client = _mock_client_with_pages([
            _pt(document_id="d1", document_title="Budget", category="budget",
                source_url="https://denvergov.org/b.pdf", uploaded_at="2026-06-01",
                parent_end_page=5),
            _pt(document_id="d1", document_title="Budget", parent_end_page=12),
            _pt(document_id="d2", document_title="Ordinance", uploaded_at="2026-06-08",
                parent_end_page=3),
        ])
        with patch.object(kb_module, "_get_client", return_value=client):
            docs = kb_module.list_documents()

        assert len(docs) == 2
        d1 = next(d for d in docs if d["document_id"] == "d1")
        assert d1["num_chunks"] == 2
        assert d1["page_count"] == 12          # max parent_end_page across chunks
        assert d1["document_title"] == "Budget"
        assert d1["source_url"] == "https://denvergov.org/b.pdf"

    def test_sorted_newest_first(self):
        kb_module._get_client.cache_clear()
        client = _mock_client_with_pages([
            _pt(document_id="old", uploaded_at="2026-06-01"),
            _pt(document_id="new", uploaded_at="2026-06-08"),
        ])
        with patch.object(kb_module, "_get_client", return_value=client):
            docs = kb_module.list_documents()
        assert [d["document_id"] for d in docs] == ["new", "old"]

    def test_normalizes_blank_source_url_and_falls_back_title(self):
        kb_module._get_client.cache_clear()
        client = _mock_client_with_pages([
            _pt(document_id="d1", document_title="", original_filename="report.pdf",
                source_url="", parent_end_page=1),
        ])
        with patch.object(kb_module, "_get_client", return_value=client):
            docs = kb_module.list_documents()
        assert docs[0]["source_url"] is None              # "" → None
        assert docs[0]["document_title"] == "report.pdf"   # title falls back to filename

    def test_paginates_across_scroll_pages(self):
        kb_module._get_client.cache_clear()
        client = _mock_client_with_pages(
            [_pt(document_id="d1", parent_end_page=1)],
            [_pt(document_id="d2", parent_end_page=1)],
        )
        with patch.object(kb_module, "_get_client", return_value=client):
            docs = kb_module.list_documents()
        assert {d["document_id"] for d in docs} == {"d1", "d2"}
        assert client.scroll.call_count == 2
