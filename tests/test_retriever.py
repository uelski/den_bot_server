"""Unit tests for the retriever node."""

from unittest.mock import MagicMock, patch

from app.graph.nodes import retriever as retriever_module


class TestRetriever:
    def test_calls_similarity_search_with_query_and_top_k(self, catalog_doc):
        mock_store = MagicMock()
        mock_store.similarity_search.return_value = [catalog_doc]

        # Clear the lru_cache so our patch takes effect
        retriever_module._get_vector_store.cache_clear()

        with patch.object(
            retriever_module, "_get_vector_store", return_value=mock_store
        ):
            result = retriever_module.retriever({"query": "parks in denver"})

        mock_store.similarity_search.assert_called_once_with(
            "parks in denver", k=retriever_module.TOP_K
        )
        assert result == {"retrieved_docs": [catalog_doc]}

    def test_returns_empty_list_when_store_returns_nothing(self):
        mock_store = MagicMock()
        mock_store.similarity_search.return_value = []
        retriever_module._get_vector_store.cache_clear()

        with patch.object(
            retriever_module, "_get_vector_store", return_value=mock_store
        ):
            result = retriever_module.retriever({"query": "zzz nothing matches"})

        assert result == {"retrieved_docs": []}

    def test_passes_through_metadata_intact(
        self, catalog_doc, neighborhood_doc_factory
    ):
        """The retriever must not mutate docs — metadata and page_content flow through."""
        nbhd = neighborhood_doc_factory("Capitol Hill", "population")
        mock_store = MagicMock()
        mock_store.similarity_search.return_value = [catalog_doc, nbhd]
        retriever_module._get_vector_store.cache_clear()

        with patch.object(
            retriever_module, "_get_vector_store", return_value=mock_store
        ):
            result = retriever_module.retriever({"query": "demographics"})

        docs = result["retrieved_docs"]
        assert len(docs) == 2
        assert docs[1].metadata["doc_type"] == "neighborhood_demographics"
        assert docs[1].metadata["neighborhood_name"] == "Capitol Hill"
        assert docs[0].metadata["has_layers"] is True
