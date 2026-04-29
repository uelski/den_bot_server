"""Unit tests for the SSE payload builders in app.main."""

from app.main import _summarize_tool_output, build_map_viewer_links, build_sources_payload


class TestBuildSourcesPayload:
    def test_empty_docs_returns_empty(self):
        assert build_sources_payload([]) == []

    def test_skips_docs_without_base_url(self, make_doc):
        doc = make_doc(service_name="X", base_url="", hub_url="https://x.example/about")
        assert build_sources_payload([doc]) == []

    def test_catalog_doc_emits_base_fields(self, catalog_doc):
        result = build_sources_payload([catalog_doc])
        assert result == [
            {
                "service_name": "Denver Parks",
                "base_url": "https://example.arcgis.com/services/Parks/FeatureServer",
                "hub_url": "https://opendata-geospatialdenver.hub.arcgis.com/datasets/parks/about",
            }
        ]

    def test_catalog_doc_without_hub_url_omits_key(self, make_doc):
        doc = make_doc(service_name="X", base_url="https://x/FeatureServer")
        result = build_sources_payload([doc])
        assert "hub_url" not in result[0]

    def test_duplicate_catalog_docs_dedup_to_one_entry(self, catalog_doc):
        result = build_sources_payload([catalog_doc, catalog_doc, catalog_doc])
        assert len(result) == 1

    def test_neighborhood_doc_includes_neighborhood_name_and_doc_type(
        self, neighborhood_doc_factory
    ):
        doc = neighborhood_doc_factory("Capitol Hill", "population")
        result = build_sources_payload([doc])
        assert result[0]["neighborhood_name"] == "Capitol Hill"
        assert result[0]["doc_type"] == "neighborhood_demographics"

    def test_multiple_neighborhoods_each_emit_entry(self, neighborhood_doc_factory):
        docs = [
            neighborhood_doc_factory("Capitol Hill", "population"),
            neighborhood_doc_factory("Five Points", "housing"),
            neighborhood_doc_factory("Globeville", "income_poverty"),
        ]
        result = build_sources_payload(docs)
        names = [r["neighborhood_name"] for r in result]
        assert names == ["Capitol Hill", "Five Points", "Globeville"]

    def test_same_neighborhood_different_topics_dedup(self, neighborhood_doc_factory):
        """Two chunks for Capitol Hill (population + housing) should collapse to
        one source entry because neighborhood_name is identical."""
        docs = [
            neighborhood_doc_factory("Capitol Hill", "population"),
            neighborhood_doc_factory("Capitol Hill", "housing"),
        ]
        result = build_sources_payload(docs)
        assert len(result) == 1
        assert result[0]["neighborhood_name"] == "Capitol Hill"

    def test_mixed_catalog_and_neighborhood(self, catalog_doc, neighborhood_doc_factory):
        docs = [catalog_doc, neighborhood_doc_factory("Capitol Hill")]
        result = build_sources_payload(docs)
        assert len(result) == 2
        assert "neighborhood_name" not in result[0]
        assert result[1]["neighborhood_name"] == "Capitol Hill"


class TestBuildMapViewerLinks:
    def test_empty_docs_returns_empty(self):
        assert build_map_viewer_links([]) == []

    def test_docs_without_hub_url_skipped(self, make_doc):
        doc = make_doc(service_name="X", base_url="https://x/fs")
        assert build_map_viewer_links([doc]) == []

    def test_strips_trailing_about_and_slash(self, make_doc):
        doc = make_doc(
            service_name="X",
            hub_url="https://hub.example.com/datasets/abc/about",
        )
        result = build_map_viewer_links([doc])
        assert result[0]["url"] == "https://hub.example.com/datasets/abc"

    def test_label_uses_service_name(self, catalog_doc):
        result = build_map_viewer_links([catalog_doc])
        assert result[0]["label"] == "View Denver Parks map"

    def test_dedup_identical_hub_urls(self, neighborhood_doc_factory):
        docs = [
            neighborhood_doc_factory("Capitol Hill", "population"),
            neighborhood_doc_factory("Five Points", "housing"),
            neighborhood_doc_factory("Globeville", "income_poverty"),
        ]
        # All three neighborhood docs share the same hub_url
        result = build_map_viewer_links(docs)
        assert len(result) == 1

    def test_distinct_hub_urls_both_emitted(self, catalog_doc, neighborhood_doc_factory):
        docs = [catalog_doc, neighborhood_doc_factory("Capitol Hill")]
        result = build_map_viewer_links(docs)
        assert len(result) == 2


WEATHER_TOOL = "get_neighborhood_weather"
ALERTS_TOOL = "get_rtd_service_alerts"

WEATHER_KEYS = {"ok", "error", "neighborhood_name", "lat", "lon", "period_count"}
ALERTS_KEYS = {"ok", "error", "total_active", "filtered_count", "alerts_url", "sample"}


class TestSummarizeToolOutputDeterministicShape:
    """The contract: payloads for a known tool always have the full keyset for
    that tool, with `null` for missing values. Frontend can rely on this
    discriminated-union shape without existence checks."""

    # --- Weather --------------------------------------------------------------

    def test_weather_success_full_keyset_populated(self):
        output = {
            "neighborhood_name": "Five Points",
            "lat": 39.76,
            "lon": -104.97,
            "periods": [{"name": "Tonight"}, {"name": "Tomorrow"}],
        }
        result = _summarize_tool_output(WEATHER_TOOL, output)
        assert set(result.keys()) == WEATHER_KEYS
        assert result["ok"] is True
        assert result["error"] is None
        assert result["neighborhood_name"] == "Five Points"
        assert result["lat"] == 39.76
        assert result["lon"] == -104.97
        assert result["period_count"] == 2

    def test_weather_error_keeps_full_keyset_with_nulls(self):
        result = _summarize_tool_output(WEATHER_TOOL, {"error": "no centroid"})
        assert set(result.keys()) == WEATHER_KEYS
        assert result["ok"] is False
        assert result["error"] == "no centroid"
        for key in ("neighborhood_name", "lat", "lon", "period_count"):
            assert result[key] is None

    def test_weather_with_some_fields_missing_gets_nulls_for_those(self):
        output = {"neighborhood_name": "Capitol Hill"}  # lat/lon/periods missing
        result = _summarize_tool_output(WEATHER_TOOL, output)
        assert set(result.keys()) == WEATHER_KEYS
        assert result["neighborhood_name"] == "Capitol Hill"
        assert result["lat"] is None
        assert result["lon"] is None
        assert result["period_count"] is None

    def test_weather_none_output_full_keyset_with_error(self):
        result = _summarize_tool_output(WEATHER_TOOL, None)
        assert set(result.keys()) == WEATHER_KEYS
        assert result["ok"] is False
        assert result["error"] == "no output"
        assert result["neighborhood_name"] is None

    # --- Alerts ---------------------------------------------------------------

    def test_alerts_success_full_keyset_populated(self):
        output = {
            "alerts": [],
            "total_active": 0,
            "filtered_count": 0,
            "alerts_url": "https://app.rtd-denver.com/alerts",
        }
        result = _summarize_tool_output(ALERTS_TOOL, output)
        assert set(result.keys()) == ALERTS_KEYS
        assert result["ok"] is True
        assert result["total_active"] == 0
        assert result["alerts_url"] == "https://app.rtd-denver.com/alerts"
        assert result["sample"] == []

    def test_alerts_error_keeps_full_keyset_with_nulls(self):
        result = _summarize_tool_output(
            ALERTS_TOOL, {"error": "RTD endpoint unreachable"}
        )
        assert set(result.keys()) == ALERTS_KEYS
        assert result["ok"] is False
        assert result["error"] == "RTD endpoint unreachable"
        for key in ("total_active", "filtered_count", "alerts_url", "sample"):
            assert result[key] is None

    def test_alerts_sample_truncated_to_two(self):
        output = {
            "alerts": [
                {"header": f"alert {i}", "effect": "DETOUR", "affected_routes": [f"R{i}"], "affected_stops": []}
                for i in range(5)
            ],
            "total_active": 47,
            "filtered_count": 5,
            "alerts_url": "https://app.rtd-denver.com/alerts",
        }
        result = _summarize_tool_output(ALERTS_TOOL, output)
        assert result["total_active"] == 47
        assert result["filtered_count"] == 5
        assert len(result["sample"]) == 2
        assert result["sample"][0]["header"] == "alert 0"
        assert result["sample"][1]["header"] == "alert 1"

    def test_alerts_sample_carries_routes_and_effect(self):
        output = {
            "alerts": [
                {
                    "header": "W Line single tracking",
                    "effect": "SIGNIFICANT_DELAYS",
                    "affected_routes": ["W"],
                    "affected_stops": ["S1"],
                }
            ],
            "total_active": 1,
            "filtered_count": 1,
            "alerts_url": "https://app.rtd-denver.com/alerts",
        }
        result = _summarize_tool_output(ALERTS_TOOL, output)
        sample = result["sample"][0]
        assert sample["header"] == "W Line single tracking"
        assert sample["effect"] == "SIGNIFICANT_DELAYS"
        assert sample["affected_routes"] == ["W"]
        assert sample["affected_stops"] == ["S1"]

    def test_alerts_long_header_truncated(self):
        output = {
            "alerts": [{"header": "x" * 500, "effect": "OTHER_EFFECT",
                        "affected_routes": [], "affected_stops": []}],
            "total_active": 1,
            "filtered_count": 1,
            "alerts_url": "https://app.rtd-denver.com/alerts",
        }
        result = _summarize_tool_output(ALERTS_TOOL, output)
        assert len(result["sample"][0]["header"]) == 160

    # --- Unknown tool fallback -----------------------------------------------

    def test_unknown_tool_returns_minimal_shape(self):
        result = _summarize_tool_output("not_a_real_tool", {"some": "data"})
        assert set(result.keys()) == {"ok", "error"}
        assert result["ok"] is True
        assert result["error"] is None

    def test_unknown_tool_with_error_returns_minimal_error_shape(self):
        result = _summarize_tool_output("not_a_real_tool", {"error": "fail"})
        assert result == {"ok": False, "error": "fail"}

    def test_unknown_tool_none_output(self):
        result = _summarize_tool_output("not_a_real_tool", None)
        assert result == {"ok": False, "error": "no output"}

    # --- Non-dict input branch -----------------------------------------------

    def test_non_dict_output_for_known_tool_keeps_keyset_with_nulls(self):
        result = _summarize_tool_output(WEATHER_TOOL, "just a string")
        assert set(result.keys()) == WEATHER_KEYS
        assert result["ok"] is True
        assert result["neighborhood_name"] is None
