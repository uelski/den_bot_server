"""rewriter node — LLM query rewriter; increments retry_count."""

import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.prompts.rewriter_prompt import REWRITER_HUMAN, REWRITER_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def rewriter(state: AgentState) -> dict:
    """Rewrite the query to improve retrieval, then increment retry_count."""
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.3)

    prompt = ChatPromptTemplate.from_messages(
        [("system", REWRITER_SYSTEM), ("human", REWRITER_HUMAN)]
    )
    chain = prompt | llm

    result = chain.invoke({"query": state["query"]})
    rewritten = result.content.strip()

    return {
        "query": rewritten,
        "retry_count": state["retry_count"] + 1,
    }
