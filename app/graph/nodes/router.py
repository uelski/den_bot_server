"""main_router node — classifies query as data_search, tool, or general."""

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
    tool = "tool"
    general = "general"


class RouterOutput(BaseModel):
    query_type: QueryType


def main_router(state: AgentState) -> dict:
    """Classify query intent. Sets two booleans the orchestrator routes on:
      - requires_rag=True  -> retriever path (catalog + neighborhood demographics)
      - needs_tool=True    -> tool_agent path (weather and future external tools)
      - both False         -> generate path (general/chitchat)
    """
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(RouterOutput)

    prompt = ChatPromptTemplate.from_messages(
        [("system", ROUTER_SYSTEM), ("human", ROUTER_HUMAN)]
    )
    chain = prompt | structured_llm

    result: RouterOutput = chain.invoke({"query": state["query"]})

    return {
        "requires_rag": result.query_type == QueryType.data_search,
        "needs_tool": result.query_type == QueryType.tool,
    }
