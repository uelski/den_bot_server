"""Unit tests for the multi-turn behavior of the generator node.

Verifies that prior conversation history is sliced via truncate_history
and threaded through the prompt template, and that the generator records
both the current HumanMessage and the new AIMessage so the conversation
accumulates correctly via the add_messages reducer.

The LLM call is mocked — no real Gemini traffic. We patch
ChatGoogleGenerativeAI at the module level so the test exercises the
prompt-construction path end to end without waiting on an API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from app.graph.nodes import generator as generator_module


def _stub_llm(reply: str = "stubbed reply") -> MagicMock:
    """A MagicMock standing in for ChatGoogleGenerativeAI.

    The chain is `prompt | llm`; LangChain wires up the pipe via the
    Runnable interface. We patch at the LLM constructor so each call to
    `ChatGoogleGenerativeAI(...)` returns the mock, and `(prompt | llm).ainvoke(...)`
    returns an AIMessage."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=reply))
    # When `prompt | llm` runs, LangChain's Runnable mechanics call
    # llm.invoke / ainvoke. For our purposes we only need ainvoke to work.
    # Patching at chain level is simpler — see the patch below.
    return llm


def _retrieved_docs() -> list[Document]:
    return [
        Document(
            page_content="Capitol Hill is a Denver neighborhood.",
            metadata={"service_name": "Test Service", "hub_url": "https://hub.example/x"},
        ),
    ]


@pytest.mark.asyncio
async def test_generator_records_human_and_ai_messages():
    """The generator must persist BOTH halves of the turn so the multi-turn
    history accumulates correctly via add_messages."""
    state = {
        "query": "What's in Capitol Hill?",
        "messages": [],  # first turn
        "requires_rag": True,
        "retrieved_docs": _retrieved_docs(),
        "needs_scrape": False,
        "tool_results": None,
    }

    fake_response = MagicMock()
    fake_response.content = "Capitol Hill is a residential Denver neighborhood."
    fake_chain = MagicMock()
    fake_chain.ainvoke = AsyncMock(return_value=fake_response)

    with patch.object(generator_module, "ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.graph.nodes.generator.ChatPromptTemplate.from_messages") as mock_prompt:
        # The chain is prompt | llm; mock the whole chain to skip langchain plumbing.
        mock_prompt_obj = MagicMock()
        mock_prompt_obj.__or__ = MagicMock(return_value=fake_chain)
        mock_prompt.return_value = mock_prompt_obj
        result = await generator_module.generator(state)

    msgs = result["messages"]
    assert len(msgs) == 2
    assert isinstance(msgs[0], HumanMessage)
    assert msgs[0].content == "What's in Capitol Hill?"
    assert isinstance(msgs[1], AIMessage)
    assert msgs[1].content == "Capitol Hill is a residential Denver neighborhood."


@pytest.mark.asyncio
async def test_generator_threads_prior_history_through_prompt():
    """When the checkpointer has loaded prior turns into state.messages,
    the generator should pass them as the `history` placeholder var on
    the prompt's ainvoke call (truncated by MEMORY_HISTORY_TURNS)."""
    prior = [
        HumanMessage(content="Tell me about Capitol Hill"),
        AIMessage(content="Capitol Hill is a residential neighborhood..."),
    ]
    state = {
        "query": "What about Five Points?",
        "messages": prior,
        "requires_rag": True,
        "retrieved_docs": _retrieved_docs(),
        "needs_scrape": False,
        "tool_results": None,
    }

    fake_response = MagicMock()
    fake_response.content = "Five Points is..."
    fake_chain = MagicMock()
    fake_chain.ainvoke = AsyncMock(return_value=fake_response)

    with patch.object(generator_module, "ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.graph.nodes.generator.ChatPromptTemplate.from_messages") as mock_prompt:
        mock_prompt_obj = MagicMock()
        mock_prompt_obj.__or__ = MagicMock(return_value=fake_chain)
        mock_prompt.return_value = mock_prompt_obj
        await generator_module.generator(state)

    # The chain.ainvoke call should have received the prior messages under `history`.
    fake_chain.ainvoke.assert_awaited_once()
    call_args = fake_chain.ainvoke.await_args.args[0]
    assert "history" in call_args
    assert call_args["history"] == prior
    assert call_args["query"] == "What about Five Points?"


@pytest.mark.asyncio
async def test_generator_truncates_long_history(monkeypatch):
    """When the conversation exceeds MEMORY_HISTORY_TURNS, only the last N
    messages should reach the prompt — the rest stay in Redis but aren't
    sent to the LLM (token budget control)."""
    from app.graph.memory import MEMORY_HISTORY_TURNS_ENV
    monkeypatch.setenv(MEMORY_HISTORY_TURNS_ENV, "4")

    long_history = []
    for i in range(10):
        long_history.append(HumanMessage(content=f"q{i}"))
        long_history.append(AIMessage(content=f"a{i}"))

    state = {
        "query": "next question",
        "messages": long_history,  # 20 messages
        "requires_rag": True,
        "retrieved_docs": _retrieved_docs(),
        "needs_scrape": False,
        "tool_results": None,
    }

    fake_response = MagicMock()
    fake_response.content = "answer"
    fake_chain = MagicMock()
    fake_chain.ainvoke = AsyncMock(return_value=fake_response)

    with patch.object(generator_module, "ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.graph.nodes.generator.ChatPromptTemplate.from_messages") as mock_prompt:
        mock_prompt_obj = MagicMock()
        mock_prompt_obj.__or__ = MagicMock(return_value=fake_chain)
        mock_prompt.return_value = mock_prompt_obj
        await generator_module.generator(state)

    history_arg = fake_chain.ainvoke.await_args.args[0]["history"]
    assert len(history_arg) == 4
    # Should be the last 4 of the 20 — q8, a8, q9, a9.
    assert history_arg[0].content == "q8"
    assert history_arg[-1].content == "a9"


@pytest.mark.asyncio
async def test_generator_handles_empty_history():
    """No prior history (first turn or no checkpointer) — the prompt still
    gets `history: []`, not a missing key."""
    state = {
        "query": "first question",
        "messages": [],
        "requires_rag": True,
        "retrieved_docs": _retrieved_docs(),
        "needs_scrape": False,
        "tool_results": None,
    }

    fake_response = MagicMock()
    fake_response.content = "answer"
    fake_chain = MagicMock()
    fake_chain.ainvoke = AsyncMock(return_value=fake_response)

    with patch.object(generator_module, "ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.graph.nodes.generator.ChatPromptTemplate.from_messages") as mock_prompt:
        mock_prompt_obj = MagicMock()
        mock_prompt_obj.__or__ = MagicMock(return_value=fake_chain)
        mock_prompt.return_value = mock_prompt_obj
        await generator_module.generator(state)

    history_arg = fake_chain.ainvoke.await_args.args[0]["history"]
    assert history_arg == []


@pytest.mark.asyncio
async def test_tool_path_also_threads_history():
    """Tool-driven generations (weather, RTD, search_denver_gov, etc.)
    should also use prior conversation context — same pattern as RAG path."""
    prior = [
        HumanMessage(content="What's the weather in Capitol Hill?"),
        AIMessage(content="Capitol Hill is sunny and 72."),
    ]
    state = {
        "query": "what about Five Points?",
        "messages": prior,
        "requires_rag": False,
        "retrieved_docs": [],
        "needs_scrape": False,
        "tool_results": [
            {"tool": "get_neighborhood_weather", "result": {"neighborhood_name": "Five Points"}},
        ],
    }

    fake_response = MagicMock()
    fake_response.content = "Five Points is also sunny."
    fake_chain = MagicMock()
    fake_chain.ainvoke = AsyncMock(return_value=fake_response)

    with patch.object(generator_module, "ChatGoogleGenerativeAI", return_value=MagicMock()), \
         patch("app.graph.nodes.generator.ChatPromptTemplate.from_messages") as mock_prompt:
        mock_prompt_obj = MagicMock()
        mock_prompt_obj.__or__ = MagicMock(return_value=fake_chain)
        mock_prompt.return_value = mock_prompt_obj
        result = await generator_module.generator(state)

    history_arg = fake_chain.ainvoke.await_args.args[0]["history"]
    assert history_arg == prior
    # Tool-path generator records the human + ai messages too.
    assert len(result["messages"]) == 2
    assert isinstance(result["messages"][0], HumanMessage)
    assert result["messages"][0].content == "what about Five Points?"
    assert isinstance(result["messages"][1], AIMessage)
