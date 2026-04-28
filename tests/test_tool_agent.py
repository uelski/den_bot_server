"""Unit tests for the tool_agent node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.graph.nodes import tool_agent as tool_agent_module


def _llm_response_with_tool_call(name: str, args: dict, call_id: str = "call_1"):
    """Build a fake LLM response that triggers a tool call."""
    response = MagicMock()
    response.tool_calls = [{"name": name, "args": args, "id": call_id}]
    response.content = ""
    return response


def _llm_response_final(content: str = "Done."):
    """LLM finished — no tool calls, just a final message."""
    response = MagicMock()
    response.tool_calls = []
    response.content = content
    return response


@pytest.mark.asyncio
async def test_no_tool_calls_returns_empty_results():
    """When the LLM doesn't call any tool on the first turn, tool_results is []."""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=_llm_response_final("hi"))

    mock_chat_cls = MagicMock()
    mock_chat_cls.return_value.bind_tools.return_value = mock_llm

    with patch.object(tool_agent_module, "ChatGoogleGenerativeAI", mock_chat_cls):
        result = await tool_agent_module.tool_agent({"query": "hi"})

    assert result == {"tool_results": []}


@pytest.mark.asyncio
async def test_single_tool_call_collects_result():
    """LLM calls the weather tool once, then finishes."""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        _llm_response_with_tool_call(
            "get_neighborhood_weather",
            {"query": "weather in RiNo", "max_periods": 4},
        ),
        _llm_response_final("Forecast summary."),
    ])
    mock_chat_cls = MagicMock()
    mock_chat_cls.return_value.bind_tools.return_value = mock_llm

    fake_tool = MagicMock()
    fake_tool.ainvoke = AsyncMock(return_value={"neighborhood_name": "Five Points", "periods": []})

    with patch.object(tool_agent_module, "ChatGoogleGenerativeAI", mock_chat_cls), \
         patch.dict(
             tool_agent_module._TOOL_BY_NAME,
             {"get_neighborhood_weather": fake_tool},
             clear=False,
         ):
        result = await tool_agent_module.tool_agent({"query": "weather in RiNo"})

    assert len(result["tool_results"]) == 1
    entry = result["tool_results"][0]
    assert entry["tool"] == "get_neighborhood_weather"
    assert entry["args"] == {"query": "weather in RiNo", "max_periods": 4}
    assert entry["result"]["neighborhood_name"] == "Five Points"
    fake_tool.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_error_dict():
    """If the LLM calls a tool we haven't registered, capture the error rather than crash."""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        _llm_response_with_tool_call("nonexistent_tool", {}),
        _llm_response_final(),
    ])
    mock_chat_cls = MagicMock()
    mock_chat_cls.return_value.bind_tools.return_value = mock_llm

    with patch.object(tool_agent_module, "ChatGoogleGenerativeAI", mock_chat_cls):
        result = await tool_agent_module.tool_agent({"query": "x"})

    assert len(result["tool_results"]) == 1
    assert "unknown tool" in result["tool_results"][0]["result"]["error"]


@pytest.mark.asyncio
async def test_tool_exception_captured_not_raised():
    """If a tool raises, the error is captured in the result dict."""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[
        _llm_response_with_tool_call("get_neighborhood_weather", {"query": "x"}),
        _llm_response_final(),
    ])
    mock_chat_cls = MagicMock()
    mock_chat_cls.return_value.bind_tools.return_value = mock_llm

    fake_tool = MagicMock()
    fake_tool.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    with patch.object(tool_agent_module, "ChatGoogleGenerativeAI", mock_chat_cls), \
         patch.dict(
             tool_agent_module._TOOL_BY_NAME,
             {"get_neighborhood_weather": fake_tool},
             clear=False,
         ):
        result = await tool_agent_module.tool_agent({"query": "x"})

    assert "boom" in result["tool_results"][0]["result"]["error"]


@pytest.mark.asyncio
async def test_max_iterations_caps_loop():
    """If the LLM keeps calling tools forever, the loop terminates at MAX_ITERATIONS."""
    mock_llm = MagicMock()
    # Always return a tool call — never finalizes
    mock_llm.ainvoke = AsyncMock(
        return_value=_llm_response_with_tool_call("get_neighborhood_weather", {"query": "x"})
    )
    mock_chat_cls = MagicMock()
    mock_chat_cls.return_value.bind_tools.return_value = mock_llm

    fake_tool = MagicMock()
    fake_tool.ainvoke = AsyncMock(return_value={"ok": True})

    with patch.object(tool_agent_module, "ChatGoogleGenerativeAI", mock_chat_cls), \
         patch.dict(
             tool_agent_module._TOOL_BY_NAME,
             {"get_neighborhood_weather": fake_tool},
             clear=False,
         ):
        result = await tool_agent_module.tool_agent({"query": "x"})

    assert mock_llm.ainvoke.call_count == tool_agent_module.MAX_ITERATIONS
    assert len(result["tool_results"]) == tool_agent_module.MAX_ITERATIONS
