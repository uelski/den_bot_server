"""intent_router node — LLM intent classifier + has_layers metadata check."""

import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.graph.state import AgentState
from app.prompts.intent_prompt import INTENT_HUMAN, INTENT_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")


class IntentOutput(BaseModel):
    needs_map: bool


def intent_router(state: AgentState) -> dict:
    """Set needs_scrape=True when query needs map/field detail AND a layer doc is available."""
    # Check if any retrieved doc has has_layers=True
    has_layers_doc = any(
        doc.metadata.get("has_layers", False) for doc in state["retrieved_docs"]
    )

    if not has_layers_doc:
        return {"needs_scrape": False}

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(IntentOutput)

    prompt = ChatPromptTemplate.from_messages(
        [("system", INTENT_SYSTEM), ("human", INTENT_HUMAN)]
    )
    chain = prompt | structured_llm

    result: IntentOutput = chain.invoke({"query": state["query"]})
    return {"needs_scrape": result.needs_map}
