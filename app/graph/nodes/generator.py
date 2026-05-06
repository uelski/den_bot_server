"""generator node — async streaming LLM response.

Multi-turn memory: when the graph is compiled with a checkpointer (see
app/graph/memory.py), prior messages are loaded into `state["messages"]`
automatically by LangGraph. The generator slices off the last
MEMORY_HISTORY_TURNS messages and injects them via a MessagesPlaceholder
in the prompt template, so the LLM has conversational context for
follow-up questions like "what about Five Points?". The full history
remains in Redis; truncation is purely a prompt-token-budget control.

The generator records BOTH halves of the current turn (HumanMessage +
AIMessage) into state.messages. The `add_messages` reducer on AgentState
appends them to whatever the checkpointer loaded, and they persist to
Redis on the next checkpoint write.
"""

import json
import logging
import os

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.memory import truncate_history
from app.graph.state import AgentState
from app.prompts.generator_prompt import (
    GENERATOR_HUMAN,
    GENERATOR_HUMAN_TOOL,
    GENERATOR_SYSTEM_GENERAL,
    GENERATOR_SYSTEM_HEDGE,
    GENERATOR_SYSTEM_STANDARD,
    GENERATOR_SYSTEM_TOOL,
)

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _format_docs(docs) -> str:
    parts = []
    for d in docs:
        name = d.metadata.get("service_name", "Unknown")
        hub_url = d.metadata.get("hub_url")
        neighborhood_name = d.metadata.get("neighborhood_name")
        topic = d.metadata.get("topic")
        if neighborhood_name:
            header = f"[{name} — {neighborhood_name}"
            if topic:
                header += f" ({topic.replace('_', ' ').title()})"
            header += "]"
        else:
            header = f"[{name}]"
        if hub_url:
            header += f" (Hub page: {hub_url})"
        parts.append(f"{header}\n{d.page_content}")
    return "\n\n---\n\n".join(parts)


def _build_prompt_with_history(system_prompt: str, human_template: str) -> ChatPromptTemplate:
    """Standard 3-part conversation shape: system, prior history, current
    human turn. The MessagesPlaceholder is consumed via a `history` key in
    the invoke dict; passing [] is fine for first-turn or no-checkpointer
    runs (no prior messages render)."""
    return ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("history"),
        ("human", human_template),
    ])


async def generator(state: AgentState) -> dict:
    """Stream an LLM response; select prompt based on query path.

    Returns both a HumanMessage (the current user turn) and the AIMessage
    (the response) so the conversation history accumulates correctly via
    the add_messages reducer on AgentState.messages.
    """
    has_scrape = state.get("needs_scrape")
    tool_results = state.get("tool_results")
    query = state["query"]
    # Prior messages from earlier turns — empty list when no checkpointer
    # is configured or this is the first turn of a thread.
    raw_history = state.get("messages") or []
    prior_history = truncate_history(raw_history)
    logger.info(
        "generator: query=%r, raw_history_count=%d, truncated_history_count=%d",
        query[:60], len(raw_history), len(prior_history),
    )

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.2, streaming=True)

    if tool_results:
        prompt = _build_prompt_with_history(GENERATOR_SYSTEM_TOOL, GENERATOR_HUMAN_TOOL)
        result = await (prompt | llm).ainvoke({
            "query": query,
            "tool_results": json.dumps(tool_results, indent=2, default=str),
            "history": prior_history,
        })
        return {
            "messages": [HumanMessage(content=query), AIMessage(content=result.content)],
        }

    if not state.get("requires_rag"):
        system_prompt = GENERATOR_SYSTEM_GENERAL
    elif has_scrape:
        system_prompt = GENERATOR_SYSTEM_HEDGE
    else:
        system_prompt = GENERATOR_SYSTEM_STANDARD

    prompt = _build_prompt_with_history(system_prompt, GENERATOR_HUMAN)
    chain = prompt | llm

    result = await chain.ainvoke({
        "query": query,
        "documents": _format_docs(state["retrieved_docs"]),
        "history": prior_history,
    })

    return {
        "messages": [HumanMessage(content=query), AIMessage(content=result.content)],
    }
