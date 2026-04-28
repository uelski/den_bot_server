"""generator node — async streaming LLM response."""

import json
import os

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.prompts.generator_prompt import (
    GENERATOR_HUMAN,
    GENERATOR_HUMAN_TOOL,
    GENERATOR_SYSTEM_GENERAL,
    GENERATOR_SYSTEM_HEDGE,
    GENERATOR_SYSTEM_STANDARD,
    GENERATOR_SYSTEM_TOOL,
)

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


async def generator(state: AgentState) -> dict:
    """Stream an LLM response; select prompt based on query path."""
    has_scrape = state.get("needs_scrape")
    tool_results = state.get("tool_results")

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.2, streaming=True)

    if tool_results:
        # Tool path — summarize the tool output(s)
        prompt = ChatPromptTemplate.from_messages(
            [("system", GENERATOR_SYSTEM_TOOL), ("human", GENERATOR_HUMAN_TOOL)]
        )
        result = await (prompt | llm).ainvoke({
            "query": state["query"],
            "tool_results": json.dumps(tool_results, indent=2, default=str),
        })
        return {"messages": [AIMessage(content=result.content)]}

    if not state.get("requires_rag"):
        system_prompt = GENERATOR_SYSTEM_GENERAL
    elif has_scrape:
        system_prompt = GENERATOR_SYSTEM_HEDGE
    else:
        system_prompt = GENERATOR_SYSTEM_STANDARD

    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", GENERATOR_HUMAN)]
    )
    chain = prompt | llm

    result = await chain.ainvoke({
        "query": state["query"],
        "documents": _format_docs(state["retrieved_docs"]),
    })

    return {"messages": [AIMessage(content=result.content)]}
