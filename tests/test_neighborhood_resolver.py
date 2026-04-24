"""Unit tests for app.neighborhoods.resolver."""

from unittest.mock import MagicMock, patch

import pytest

from app.neighborhoods import resolver as resolver_module
from app.neighborhoods.resolver import (
    ALIASES,
    OFFICIAL_NAMES,
    ResolvedNeighborhood,
    clear_cache,
    resolve,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean memoization cache."""
    clear_cache()
    yield
    clear_cache()


class TestOfficialNamesLoading:
    def test_loads_78_names_from_geojson(self):
        assert len(OFFICIAL_NAMES) == 78

    def test_names_include_known_anchors(self):
        for expected in ["Capitol Hill", "Five Points", "Washington Park", "Union Station"]:
            assert expected in OFFICIAL_NAMES

    def test_names_are_sorted(self):
        assert OFFICIAL_NAMES == sorted(OFFICIAL_NAMES)


class TestAliasesAreValid:
    def test_every_alias_target_is_an_official_name(self):
        invalid = [v for v in ALIASES.values() if v not in OFFICIAL_NAMES]
        assert invalid == []


class TestTier1AliasFastPath:
    def test_exact_alias_returns_high_confidence(self):
        result = resolve("rino")
        assert result.name == "Five Points"
        assert result.confidence == "high"

    def test_alias_is_case_insensitive(self):
        assert resolve("RiNo").name == "Five Points"
        assert resolve("RINO").name == "Five Points"

    def test_wash_park_alias(self):
        assert resolve("wash park").name == "Washington Park"

    def test_cap_hill_alias(self):
        assert resolve("Cap Hill").name == "Capitol Hill"

    def test_lodo_alias(self):
        assert resolve("LoDo").name == "Union Station"

    def test_alias_skips_llm_call(self):
        """Tier 1 must not invoke the LLM layer."""
        with patch.object(resolver_module, "_llm_resolve") as mock_llm:
            result = resolve("wash park")
        mock_llm.assert_not_called()
        assert result.name == "Washington Park"


class TestTier2OfficialNameMatch:
    def test_exact_official_name(self):
        result = resolve("Capitol Hill")
        assert result.name == "Capitol Hill"
        assert result.confidence == "high"

    def test_official_name_is_case_insensitive(self):
        assert resolve("capitol hill").name == "Capitol Hill"
        assert resolve("CAPITOL HILL").name == "Capitol Hill"

    def test_official_name_skips_llm_call(self):
        with patch.object(resolver_module, "_llm_resolve") as mock_llm:
            resolve("Five Points")
        mock_llm.assert_not_called()

    def test_whitespace_is_stripped(self):
        assert resolve("  Capitol Hill  ").name == "Capitol Hill"


class TestTier3LLMFallback:
    def test_unknown_input_falls_through_to_llm(self):
        llm_return = ResolvedNeighborhood(
            name="Five Points",
            confidence="high",
            reasoning="Recognized location cue '30th and Downing' as Five Points.",
        )
        with patch.object(resolver_module, "_llm_resolve", return_value=llm_return) as mock_llm:
            result = resolve("weather near 30th and Downing")

        mock_llm.assert_called_once_with("weather near 30th and downing")
        assert result.name == "Five Points"
        assert result.confidence == "high"

    def test_llm_returned_invalid_name_coerced_to_null(self):
        """Real Gemini occasionally invents names despite the prompt. The resolver
        must validate and reject anything not in OFFICIAL_NAMES."""
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = ResolvedNeighborhood(
            name="Made Up Neighborhood",
            confidence="high",
            reasoning="Hallucinated name.",
        )
        clear_cache()
        with patch.object(resolver_module, "_build_chain", return_value=mock_chain):
            result = resolver_module._llm_resolve("some weird query")

        assert result.name is None
        assert result.confidence == "low"
        assert "non-official" in result.reasoning.lower()

    def test_llm_exception_returns_null_not_crash(self):
        mock_chain = MagicMock()
        mock_chain.invoke.side_effect = RuntimeError("api down")
        with patch.object(resolver_module, "_build_chain", return_value=mock_chain):
            result = resolver_module._llm_resolve("anything")

        assert result.name is None
        assert result.confidence == "low"
        assert "failed" in result.reasoning.lower()


class TestCaching:
    def test_repeat_queries_hit_cache(self):
        """Second call with identical normalized query should not invoke the LLM."""
        llm_return = ResolvedNeighborhood(
            name="Five Points", confidence="high", reasoning="cached test"
        )
        with patch.object(
            resolver_module, "_llm_resolve", return_value=llm_return
        ) as mock_llm:
            resolve("some novel phrase")
            resolve("some novel phrase")
            resolve("SOME novel phrase")  # normalizes to same key

        assert mock_llm.call_count == 1

    def test_different_queries_dont_share_cache(self):
        llm_return = ResolvedNeighborhood(
            name="Five Points", confidence="high", reasoning=""
        )
        with patch.object(
            resolver_module, "_llm_resolve", return_value=llm_return
        ) as mock_llm:
            resolve("first phrase")
            resolve("second phrase")

        assert mock_llm.call_count == 2


class TestEmptyOrGarbageInput:
    def test_empty_string(self):
        result = resolve("")
        assert result.name is None
        assert result.confidence == "low"

    def test_whitespace_only(self):
        result = resolve("   ")
        assert result.name is None

    def test_empty_short_circuits_before_llm(self):
        with patch.object(resolver_module, "_llm_resolve") as mock_llm:
            resolve("")
        mock_llm.assert_not_called()


class TestReturnType:
    def test_always_returns_resolved_neighborhood_model(self):
        """Contract: resolve() never returns None directly. Callers can rely on
        .name being None for unresolvable queries but always getting a model."""
        for query in ["rino", "Capitol Hill", "gibberish xyz"]:
            with patch.object(
                resolver_module,
                "_llm_resolve",
                return_value=ResolvedNeighborhood(name=None, confidence="low", reasoning=""),
            ):
                result = resolve(query)
            assert isinstance(result, ResolvedNeighborhood)
