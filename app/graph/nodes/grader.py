"""grader node — LLM relevance grader with structured output."""

import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.graph.state import AgentState
from app.prompts.grader_prompt import GRADER_HUMAN, GRADER_SYSTEM

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


class GraderOutput(BaseModel):
    relevant: bool


def _format_docs(docs) -> str:
    return "\n\n---\n\n".join(
        f"[{d.metadata.get('service_name', 'Unknown')}]\n{d.page_content}" for d in docs
    )


def grader(state: AgentState) -> dict:
    """Determine if retrieved docs are relevant to the query.

    Grades against `search_query` (the history-resolved standalone form
    used for retrieval) so a bare follow-up like "how about Five Points?"
    isn't compared against docs that were correctly fetched using the
    condensed "parks in Five Points neighborhood Denver".
    """
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(GraderOutput)

    prompt = ChatPromptTemplate.from_messages(
        [("system", GRADER_SYSTEM), ("human", GRADER_HUMAN)]
    )
    chain = prompt | structured_llm

    grade_query = state.get("search_query") or state["query"]
    result: GraderOutput = chain.invoke(
        {
            "query": grade_query,
            "documents": _format_docs(state["retrieved_docs"]),
        }
    )
    return {"docs_relevant": result.relevant}
