"""generator node — async streaming LLM response."""

import os

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.prompts.generator_prompt import (
    GENERATOR_HUMAN,
    GENERATOR_HUMAN_PG,
    GENERATOR_SYSTEM_GENERAL,
    GENERATOR_SYSTEM_HEDGE,
    GENERATOR_SYSTEM_PG_DATA,
    GENERATOR_SYSTEM_PG_DATA_HEDGE,
    GENERATOR_SYSTEM_STANDARD,
)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _format_docs(docs) -> str:
    parts = []
    for d in docs:
        name = d.metadata.get("service_name", "Unknown")
        hub_url = d.metadata.get("hub_url")
        header = f"[{name}]"
        if hub_url:
            header += f" (Hub page: {hub_url})"
        parts.append(f"{header}\n{d.page_content}")
    return "\n\n---\n\n".join(parts)


def _format_pg_results(rows: list[dict]) -> str:
    if not rows:
        return "(no results)"
    columns = list(rows[0].keys())
    header = " | ".join(columns)
    separator = "-|-".join("-" * len(c) for c in columns)
    lines = [header, separator]
    for row in rows:
        lines.append(" | ".join(str(row.get(c, "")) for c in columns))
    return "\n".join(lines)


async def generator(state: AgentState) -> dict:
    """Stream an LLM response; select prompt based on query path."""
    has_pg = state.get("needs_pg_query") and state.get("pg_query_results") is not None
    has_scrape = state.get("needs_scrape")

    if not state.get("requires_rag"):
        system_prompt = GENERATOR_SYSTEM_GENERAL
    elif has_pg and has_scrape:
        system_prompt = GENERATOR_SYSTEM_PG_DATA_HEDGE
    elif has_pg:
        system_prompt = GENERATOR_SYSTEM_PG_DATA
    elif has_scrape:
        system_prompt = GENERATOR_SYSTEM_HEDGE
    else:
        system_prompt = GENERATOR_SYSTEM_STANDARD

    human_template = GENERATOR_HUMAN_PG if has_pg else GENERATOR_HUMAN

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.2, streaming=True)

    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", human_template)]
    )
    chain = prompt | llm

    invoke_vars = {
        "query": state["query"],
        "documents": _format_docs(state["retrieved_docs"]),
    }
    if has_pg:
        invoke_vars["pg_data"] = _format_pg_results(state["pg_query_results"])

    result = await chain.ainvoke(invoke_vars)

    return {"messages": [AIMessage(content=result.content)]}
