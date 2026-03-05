"""orchestrator.py — StateGraph assembly and edge wiring."""

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.generator import generator
from app.graph.nodes.grader import grader
from app.graph.nodes.intent_router import intent_router
from app.graph.nodes.retriever import retriever
from app.graph.nodes.rewriter import rewriter
from app.graph.nodes.router import main_router
from app.graph.nodes.scraper import scraper
from app.graph.state import AgentState


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------


def route_after_grader(state: AgentState) -> str:
    if not state["docs_relevant"]:
        return "rewrite" if state["retry_count"] < 2 else "generate"
    return "intent_router"


def route_after_intent(state: AgentState) -> list[str]:
    if state["needs_scrape"]:
        return ["generate", "scraper"]
    return ["generate"]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph():
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node("main_router", main_router)
    builder.add_node("retriever", retriever)
    builder.add_node("grader", grader)
    builder.add_node("intent_router", intent_router)
    builder.add_node("rewriter", rewriter)
    builder.add_node("generate", generator)
    builder.add_node("scraper", scraper)

    # Entry
    builder.add_edge(START, "main_router")
    builder.add_edge("main_router", "retriever")
    builder.add_edge("retriever", "grader")

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

    # After intent router: single generate or parallel fan-out
    builder.add_conditional_edges(
        "intent_router",
        route_after_intent,
        ["generate", "scraper"],
    )

    # Terminal edges
    builder.add_edge("generate", END)
    builder.add_edge("scraper", END)

    return builder.compile()


graph = build_graph()
