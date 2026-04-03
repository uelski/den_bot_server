"""main_router node — classifies query as data search or general conversation."""

import os
from enum import Enum

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.graph.state import AgentState
from app.prompts.router_prompt import ROUTER_HUMAN, ROUTER_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


class QueryType(str, Enum):
    data_search = "data_search"
    general = "general"


class RouterOutput(BaseModel):
    query_type: QueryType


def main_router(state: AgentState) -> dict:
    """Classify query intent — sets requires_rag for downstream routing."""
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(RouterOutput)

    prompt = ChatPromptTemplate.from_messages(
        [("system", ROUTER_SYSTEM), ("human", ROUTER_HUMAN)]
    )
    chain = prompt | structured_llm

    result: RouterOutput = chain.invoke({"query": state["query"]})
    return {"requires_rag": result.query_type == QueryType.data_search}
