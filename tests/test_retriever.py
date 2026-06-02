"""Unit tests for the retriever node (async, dual-collection fan-out)."""

from unittest.mock import AsyncMock, MagicMock, patch

from app.graph.nodes import retriever as retriever_module


class TestRetriever:
    async def test_merges_catalog_and_kb_results(self, catalog_doc, kb_doc_factory):
        kb_doc = kb_doc_factory(document_id="ordinances.pdf")

        with patch.object(
            retriever_module, "_search_catalog",
            new=AsyncMock(return_value=[catalog_doc]),
        ), patch.object(
            retriever_module, "search_kb", return_value=[kb_doc]
        ) as mock_kb:
            result = await retriever_module.retriever({"query": "zoning rules"})

        docs = result["retrieved_docs"]
        assert docs == [catalog_doc, kb_doc]
        # KB search runs via asyncio.to_thread with the query + candidate pool size.
        mock_kb.assert_called_once_with("zoning rules", retriever_module.CANDIDATE_K)

    async def test_prefers_search_query_over_literal_query(self, catalog_doc):
        with patch.object(
            retriever_module, "_search_catalog",
            new=AsyncMock(return_value=[catalog_doc]),
        ) as mock_catalog, patch.object(
            retriever_module, "search_kb", return_value=[]
        ) as mock_kb:
            await retriever_module.retriever(
                {"query": "literal", "search_query": "standalone rewrite"}
            )

        mock_catalog.assert_awaited_once_with("standalone rewrite")
        mock_kb.assert_called_once_with("standalone rewrite", retriever_module.CANDIDATE_K)

    async def test_returns_empty_when_both_collections_empty(self):
        with patch.object(
            retriever_module, "_search_catalog", new=AsyncMock(return_value=[])
        ), patch.object(retriever_module, "search_kb", return_value=[]):
            result = await retriever_module.retriever({"query": "zzz no match"})

        assert result == {"retrieved_docs": []}


class TestTagCatalog:
    def test_stamps_provenance_without_clobbering(self, make_doc):
        plain = make_doc(service_name="Parks")
        already = make_doc(service_name="Other", source_collection="catalog")
        tagged = retriever_module._tag_catalog([plain, already])
        assert all(d.metadata["source_collection"] == "catalog" for d in tagged)
