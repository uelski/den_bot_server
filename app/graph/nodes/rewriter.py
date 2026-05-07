"""rewriter node — LLM query rewriter; increments retry_count."""

import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.state import AgentState
from app.prompts.rewriter_prompt import REWRITER_HUMAN, REWRITER_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def rewriter(state: AgentState) -> dict:
    """Rewrite the query to improve retrieval, then increment retry_count.

    Writes to `search_query` rather than `query` so the user's literal
    question stays stable for the generator. The retriever reads
    `search_query` first, so the next retrieval pass picks up the
    rewrite automatically.

    Uses the current `search_query` (set by the condenser) as the
    starting point if available, so we're rewriting the already
    history-resolved form rather than the bare follow-up.
    """
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.3)

    prompt = ChatPromptTemplate.from_messages(
        [("system", REWRITER_SYSTEM), ("human", REWRITER_HUMAN)]
    )
    chain = prompt | llm

    base_query = state.get("search_query") or state["query"]
    result = chain.invoke({"query": base_query})
    rewritten = result.content.strip()

    return {
        "search_query": rewritten,
        "retry_count": state["retry_count"] + 1,
    }
