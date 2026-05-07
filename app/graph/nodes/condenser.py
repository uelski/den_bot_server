"""condenser node — history-aware standalone query rewrite for retrieval.

Multi-turn follow-ups like "how about Five Points?" are meaningless to
Qdrant on their own — the prior turn (e.g. "what parks are in Denver?")
is where the real subject lives. This node reads conversation history
from state.messages and produces a self-contained `search_query` that
the retriever uses instead of the bare `query`.

The literal user `query` is left untouched so the generator still
answers the question the user actually asked, with prior messages
injected via its MessagesPlaceholder. `search_query` is throwaway
scaffolding — it never enters state.messages and never persists to
Redis.
"""

import logging
import os

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_google_genai import ChatGoogleGenerativeAI

from app.graph.memory import truncate_history
from app.graph.state import AgentState
from app.prompts.condenser_prompt import CONDENSER_HUMAN, CONDENSER_SYSTEM

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


async def condenser(state: AgentState) -> dict:
    """Rewrite the latest user query into a standalone search query.

    Fast path: when there's no prior history (first turn or no
    checkpointer), skip the LLM and reuse `query` verbatim.
    """
    query = state["query"]
    raw_history = state.get("messages") or []

    if not raw_history:
        logger.info("condenser: empty history, search_query=query (fast path)")
        return {"search_query": query}

    history = truncate_history(raw_history)

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", CONDENSER_SYSTEM),
        MessagesPlaceholder("history"),
        ("human", CONDENSER_HUMAN),
    ])
    chain = prompt | llm

    result = await chain.ainvoke({"query": query, "history": history})
    rewritten = result.content.strip()

    logger.info(
        "condenser: query=%r, search_query=%r, history_count=%d",
        query[:80], rewritten[:80], len(history),
    )

    return {"search_query": rewritten or query}
