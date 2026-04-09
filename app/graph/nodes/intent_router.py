"""intent_router node — LLM intent classifier + has_layers metadata check."""

import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.graph.state import AgentState
from app.prompts.intent_prompt import INTENT_HUMAN, INTENT_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


class IntentOutput(BaseModel):
    needs_map: bool


def intent_router(state: AgentState) -> dict:
    """Set independent flags: needs_pg_query and needs_scrape."""
    # pg_table check — pure metadata, no LLM call
    pg_doc = next(
        (doc for doc in state["retrieved_docs"] if doc.metadata.get("pg_table")),
        None,
    )
    needs_pg = pg_doc is not None
    pg_table = pg_doc.metadata["pg_table"] if pg_doc else None

    # has_layers check — existing LLM intent classification
    has_layers_doc = any(
        doc.metadata.get("has_layers", False) for doc in state["retrieved_docs"]
    )

    needs_scrape = False
    if has_layers_doc:
        llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
        structured_llm = llm.with_structured_output(IntentOutput)

        prompt = ChatPromptTemplate.from_messages(
            [("system", INTENT_SYSTEM), ("human", INTENT_HUMAN)]
        )
        chain = prompt | structured_llm

        result: IntentOutput = chain.invoke({"query": state["query"]})
        needs_scrape = result.needs_map

    return {
        "needs_pg_query": needs_pg,
        "pg_table": pg_table,
        "needs_scrape": needs_scrape,
    }
