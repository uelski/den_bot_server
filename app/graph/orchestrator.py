"""orchestrator.py — StateGraph assembly and edge wiring."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.condenser import condenser
from app.graph.nodes.generator import generator
from app.graph.nodes.grader import grader
from app.graph.nodes.intent_router import intent_router
from app.graph.nodes.reranker import reranker
from app.graph.nodes.retriever import retriever
from app.graph.nodes.rewriter import rewriter
from app.graph.nodes.router import main_router
from app.graph.nodes.scraper import scraper
from app.graph.nodes.tool_agent import tool_agent
from app.graph.state import AgentState


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------


def route_after_router(state: AgentState) -> str:
    if state.get("needs_tool"):
        return "tool_agent"
    if state["requires_rag"]:
        return "condenser"
    return "generate"


def route_after_grader(state: AgentState) -> str:
    if not state["docs_relevant"]:
        return "rewrite" if state["retry_count"] < 2 else "generate"
    return "intent_router"


def route_after_intent(state: AgentState) -> list[str]:
    """Fan out: scraper+generate in parallel, or just generate."""
    if state["needs_scrape"]:
        return ["generate", "scraper"]
    return ["generate"]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Any | None = None):
    """Compile the StateGraph. Pass `checkpointer=` to enable multi-turn
    memory (typically an AsyncRedisSaver from app/graph/memory.py); leave
    None for stateless single-turn (used by tests and the no-Redis fallback)."""
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node("main_router", main_router)
    builder.add_node("condenser", condenser)
    builder.add_node("retriever", retriever)
    builder.add_node("reranker", reranker)
    builder.add_node("grader", grader)
    builder.add_node("intent_router", intent_router)
    builder.add_node("rewriter", rewriter)
    builder.add_node("generate", generator)
    builder.add_node("scraper", scraper)
    builder.add_node("tool_agent", tool_agent)

    # Entry — router decides between RAG, tools, and direct LLM
    builder.add_edge(START, "main_router")
    builder.add_conditional_edges(
        "main_router",
        route_after_router,
        {
            "condenser": "condenser",
            "tool_agent": "tool_agent",
            "generate": "generate",
        },
    )
    builder.add_edge("condenser", "retriever")
    # retriever fans out to both collections; reranker merges before grading.
    builder.add_edge("retriever", "reranker")
    builder.add_edge("reranker", "grader")
    builder.add_edge("tool_agent", "generate")

    # After grader: retry loop or proceed to intent classification
    builder.add_conditional_edges(
        "grader",
        route_after_grader,
        {
            "rewrite": "rewriter",
            "generate": "generate",
            "intent_router": "intent_router",
        },
    )

    # Retry loop back to retriever
    builder.add_edge("rewriter", "retriever")

    # After intent router: scrape+generate in parallel or generate alone
    builder.add_conditional_edges(
        "intent_router",
        route_after_intent,
        ["generate", "scraper"],
    )

    # Terminal edges
    builder.add_edge("generate", END)
    builder.add_edge("scraper", END)

    return builder.compile(checkpointer=checkpointer)


# Module-level stateless graph — used by tests and as a fallback when
# REDIS_URL is unset. The memory-enabled graph is compiled at FastAPI
# lifespan startup in app/main.py and stored on app.state.
graph = build_graph()
