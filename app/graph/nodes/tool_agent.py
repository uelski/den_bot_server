"""tool_agent node — small ReAct loop over LangChain @tool functions.

Binds every tool in `AGENT_TOOLS` to a Gemini model, then iterates:
  1. Send the user query (and any prior tool messages) to the LLM.
  2. If the LLM returns tool_calls, execute each and append the results.
  3. Otherwise, finalize and write the gathered tool results into state.

The node populates `state.tool_results` with `{tool, args, result}` per call so
the downstream generator can summarize them. The node does NOT itself stream a
final answer — that's the generator's job (keeps SSE handling consistent).

Bounded by MAX_ITERATIONS to prevent runaway loops. In practice a single
forecast lookup completes in 1 iteration (LLM picks tool, tool runs, LLM
finishes); we allow 3 for tool chains.
"""

import logging
import os
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.tools.registry import AGENT_TOOLS

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_ITERATIONS = 3


_TOOL_BY_NAME = {t.name: t for t in AGENT_TOOLS}


async def _invoke_tool(tool_name: str, args: dict) -> Any:
    tool_fn = _TOOL_BY_NAME.get(tool_name)
    if tool_fn is None:
        return {"error": f"unknown tool {tool_name!r}"}
    try:
        return await tool_fn.ainvoke(args)
    except Exception as exc:
        logger.warning("tool %s failed: %s", tool_name, exc)
        return {"error": f"tool {tool_name} failed: {exc}"}


async def tool_agent(state: AgentState) -> dict:
    """Run the tool-calling loop and collect results into state.tool_results."""
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0).bind_tools(AGENT_TOOLS)

    messages: list = [HumanMessage(content=state["query"])]
    collected: list[dict] = []

    for iteration in range(MAX_ITERATIONS):
        response = await llm.ainvoke(messages)
        messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        for call in tool_calls:
            name = call["name"]
            args = call.get("args", {}) or {}
            result = await _invoke_tool(name, args)
            collected.append({"tool": name, "args": args, "result": result})
            messages.append(
                ToolMessage(
                    content=str(result),
                    tool_call_id=call["id"],
                )
            )
    else:
        logger.warning(
            "tool_agent hit MAX_ITERATIONS=%d without final response",
            MAX_ITERATIONS,
        )

    return {"tool_results": collected}
