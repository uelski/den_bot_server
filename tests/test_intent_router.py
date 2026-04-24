"""Unit tests for the intent_router node."""

from unittest.mock import MagicMock, patch

from app.graph.nodes import intent_router as intent_router_module


def _state(docs: list) -> dict:
    return {"query": "test query", "retrieved_docs": docs}


class TestIntentRouterNoLayers:
    def test_no_docs_returns_needs_scrape_false(self):
        result = intent_router_module.intent_router(_state([]))
        assert result == {"needs_scrape": False}

    def test_all_docs_have_layers_false_returns_needs_scrape_false(
        self, make_doc
    ):
        docs = [make_doc(service_name="X", has_layers=False)]
        result = intent_router_module.intent_router(_state(docs))
        assert result == {"needs_scrape": False}


class TestIntentRouterWithLayers:
    def test_has_layers_and_llm_says_no_map_returns_false(self, make_doc):
        docs = [make_doc(service_name="X", has_layers=True)]
        chain_result = MagicMock()
        chain_result.needs_map = False

        with patch.object(
            intent_router_module, "ChatGoogleGenerativeAI"
        ) as mock_cls:
            mock_cls.return_value.with_structured_output.return_value.invoke = (
                MagicMock()
            )
            # the prompt-pipe-chain.invoke returns an IntentOutput-like object
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = chain_result
            mock_cls.return_value.with_structured_output.return_value = mock_chain
            # our code does `prompt | structured_llm` then `.invoke`; we patch at
            # a higher level by making the structured_llm behave as the chain
            with patch("app.graph.nodes.intent_router.ChatPromptTemplate") as mock_prompt:
                mock_prompt.from_messages.return_value.__or__ = lambda self, other: mock_chain
                result = intent_router_module.intent_router(_state(docs))

        assert result == {"needs_scrape": False}

    def test_has_layers_and_llm_says_needs_map_returns_true(self, make_doc):
        docs = [make_doc(service_name="X", has_layers=True)]
        chain_result = MagicMock()
        chain_result.needs_map = True

        with patch.object(intent_router_module, "ChatGoogleGenerativeAI") as mock_cls:
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = chain_result
            mock_cls.return_value.with_structured_output.return_value = mock_chain
            with patch("app.graph.nodes.intent_router.ChatPromptTemplate") as mock_prompt:
                mock_prompt.from_messages.return_value.__or__ = lambda self, other: mock_chain
                result = intent_router_module.intent_router(_state(docs))

        assert result == {"needs_scrape": True}

    def test_mixed_docs_with_any_layers_triggers_llm_check(self, make_doc):
        """If any retrieved doc has has_layers=True, the LLM classifier runs."""
        docs = [
            make_doc(service_name="A", has_layers=False),
            make_doc(service_name="B", has_layers=True),
        ]
        chain_result = MagicMock()
        chain_result.needs_map = True

        with patch.object(intent_router_module, "ChatGoogleGenerativeAI") as mock_cls:
            mock_chain = MagicMock()
            mock_chain.invoke.return_value = chain_result
            mock_cls.return_value.with_structured_output.return_value = mock_chain
            with patch("app.graph.nodes.intent_router.ChatPromptTemplate") as mock_prompt:
                mock_prompt.from_messages.return_value.__or__ = lambda self, other: mock_chain
                result = intent_router_module.intent_router(_state(docs))

        assert result == {"needs_scrape": True}
        # Confirm the LLM was actually called
        mock_chain.invoke.assert_called_once()
