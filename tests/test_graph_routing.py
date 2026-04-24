"""Integration-ish tests for graph structure and routing logic.

Focuses on the pure conditional edge functions that drive node selection, plus
a smoke test that the compiled graph has the expected nodes. We do not exercise
the full `graph.ainvoke()` path because that requires mocking five independent
LLM modules — high maintenance cost, low marginal value over node-level tests.
"""

from app.graph.orchestrator import (
    build_graph,
    route_after_grader,
    route_after_intent,
    route_after_router,
)


class TestGraphStructure:
    def test_graph_compiles(self):
        g = build_graph()
        assert g is not None

    def test_expected_nodes_present(self):
        g = build_graph()
        node_names = set(g.get_graph().nodes.keys())
        expected = {
            "__start__",
            "main_router",
            "retriever",
            "grader",
            "intent_router",
            "rewriter",
            "generate",
            "scraper",
            "__end__",
        }
        assert expected.issubset(node_names)

    def test_pg_query_node_is_gone(self):
        """Regression guard: pg_query was removed when we migrated demographics
        to Qdrant. This test fails loudly if it ever sneaks back in."""
        g = build_graph()
        node_names = set(g.get_graph().nodes.keys())
        assert "pg_query" not in node_names


class TestRouteAfterRouter:
    def test_requires_rag_routes_to_retriever(self):
        assert route_after_router({"requires_rag": True}) == "retriever"

    def test_general_query_routes_to_generate(self):
        assert route_after_router({"requires_rag": False}) == "generate"


class TestRouteAfterGrader:
    def test_relevant_docs_go_to_intent_router(self):
        state = {"docs_relevant": True, "retry_count": 0}
        assert route_after_grader(state) == "intent_router"

    def test_irrelevant_docs_under_retry_limit_rewrite(self):
        state = {"docs_relevant": False, "retry_count": 0}
        assert route_after_grader(state) == "rewrite"

    def test_irrelevant_docs_at_retry_limit_generate_anyway(self):
        state = {"docs_relevant": False, "retry_count": 2}
        assert route_after_grader(state) == "generate"

    def test_retry_count_one_still_rewrites(self):
        state = {"docs_relevant": False, "retry_count": 1}
        assert route_after_grader(state) == "rewrite"


class TestRouteAfterIntent:
    def test_no_scrape_routes_to_generate_only(self):
        assert route_after_intent({"needs_scrape": False}) == ["generate"]

    def test_needs_scrape_fans_out_to_generate_and_scraper(self):
        result = route_after_intent({"needs_scrape": True})
        assert set(result) == {"generate", "scraper"}
        assert len(result) == 2
