"""generator node — async streaming LLM response."""

import os

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.prompts.generator_prompt import (
    GENERATOR_HUMAN,
    GENERATOR_SYSTEM_HEDGE,
    GENERATOR_SYSTEM_STANDARD,
)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _format_docs(docs) -> str:
    return "\n\n---\n\n".join(
        f"[{d.metadata.get('service_name', 'Unknown')}]\n"
        f"URL: {d.metadata.get('base_url', 'N/A')}\n\n"
        f"{d.page_content}"
        for d in docs
    )


async def generator(state: AgentState) -> dict:
    """Stream an LLM response; use hedge prompt when needs_scrape=True."""
    system_prompt = (
        GENERATOR_SYSTEM_HEDGE if state.get("needs_scrape") else GENERATOR_SYSTEM_STANDARD
    )

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.2, streaming=True)

    prompt = ChatPromptTemplate.from_messages(
        [("system", system_prompt), ("human", GENERATOR_HUMAN)]
    )
    chain = prompt | llm

    result = await chain.ainvoke(
        {
            "query": state["query"],
            "documents": _format_docs(state["retrieved_docs"]),
        }
    )

    return {"messages": [AIMessage(content=result.content)]}
