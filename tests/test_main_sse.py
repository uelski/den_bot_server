"""Unit tests for the SSE payload builders in app.main."""

from app.main import (
    _summarize_tool_output,
    build_map_viewer_links,
    build_sources_payload,
    build_tool_map_viewer_links,
)


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


ARRIVALS_TOOL = "get_rtd_next_arrivals"
VEHICLES_TOOL = "get_rtd_vehicle_positions"
ARRIVALS_KEYS = {
    "ok", "error", "stop_count", "arrival_count", "resolution_method",
    "has_realtime", "sample",
}
VEHICLES_KEYS = {
    "ok", "error", "route_short_name", "route_long_name", "vehicle_count",
    "has_realtime",
}


class TestSummarizeArrivalsAndVehicles:
    def test_arrivals_full_keyset_populated(self):
        output = {
            "matched_stops": [{"stop_id": "S1"}, {"stop_id": "S2"}],
            "arrivals": [
                {"route_short_name": "W", "headsign": "Union Station",
                 "minutes_until": 3, "stop_name": "38th & Blake"},
                {"route_short_name": "W", "headsign": "Federal Center",
                 "minutes_until": 7, "stop_name": "38th & Blake"},
            ],
            "resolution_method": "stop_name",
            "has_realtime": True,
        }
        result = _summarize_tool_output(ARRIVALS_TOOL, output)
        assert set(result.keys()) == ARRIVALS_KEYS
        assert result["ok"] is True
        assert result["stop_count"] == 2
        assert result["arrival_count"] == 2
        assert result["resolution_method"] == "stop_name"
        assert result["has_realtime"] is True
        assert len(result["sample"]) == 2

    def test_arrivals_error_keyset_with_nulls(self):
        result = _summarize_tool_output(
            ARRIVALS_TOOL, {"error": "Could not resolve a stop"}
        )
        assert set(result.keys()) == ARRIVALS_KEYS
        assert result["ok"] is False
        assert result["error"] == "Could not resolve a stop"

    def test_vehicles_full_keyset_populated(self):
        output = {
            "matched_route": {
                "route_id": "103W", "short_name": "W", "long_name": "W Line",
            },
            "vehicles": [{"vehicle_id": "v1"}, {"vehicle_id": "v2"}],
            "has_realtime": True,
        }
        result = _summarize_tool_output(VEHICLES_TOOL, output)
        assert set(result.keys()) == VEHICLES_KEYS
        assert result["ok"] is True
        assert result["route_short_name"] == "W"
        assert result["route_long_name"] == "W Line"
        assert result["vehicle_count"] == 2

    def test_vehicles_error_keyset_with_nulls(self):
        result = _summarize_tool_output(
            VEHICLES_TOOL, {"error": "Could not resolve a route"}
        )
        assert set(result.keys()) == VEHICLES_KEYS
        assert result["ok"] is False
        assert result["route_short_name"] is None


class TestBuildToolMapViewerLinks:
    def test_unknown_tool_returns_empty(self):
        assert build_tool_map_viewer_links("not_a_real_tool", {"x": 1}) == []

    def test_arrivals_emits_stop_urls_with_combined_label(self):
        output = {
            "matched_stops": [
                {
                    "stop_id": "S1", "stop_name": "38th & Blake",
                    "stop_desc": "Vehicles Travelling West",
                    "nextride_url": "https://app.rtd-denver.com/nextride/stop/S1",
                },
                {
                    "stop_id": "S2", "stop_name": "38th & Blake",
                    "stop_desc": "Vehicles Travelling East",
                    "nextride_url": "https://app.rtd-denver.com/nextride/stop/S2",
                },
            ],
        }
        links = build_tool_map_viewer_links(ARRIVALS_TOOL, output)
        assert len(links) == 2
        assert links[0]["label"] == "38th & Blake — Vehicles Travelling West"
        assert links[0]["url"].endswith("/stop/S1")
        assert links[1]["label"] == "38th & Blake — Vehicles Travelling East"

    def test_arrivals_dedupes_by_url(self):
        same = {
            "stop_id": "S1", "stop_name": "X", "stop_desc": "",
            "nextride_url": "https://app.rtd-denver.com/nextride/stop/S1",
        }
        output = {"matched_stops": [same, same]}
        assert len(build_tool_map_viewer_links(ARRIVALS_TOOL, output)) == 1

    def test_arrivals_caps_at_four(self):
        stops = [
            {
                "stop_id": f"S{i}", "stop_name": f"Stop {i}", "stop_desc": "",
                "nextride_url": f"https://app.rtd-denver.com/nextride/stop/S{i}",
            }
            for i in range(7)
        ]
        links = build_tool_map_viewer_links(ARRIVALS_TOOL, {"matched_stops": stops})
        assert len(links) == 4

    def test_arrivals_skips_when_error(self):
        output = {
            "matched_stops": [
                {
                    "stop_id": "S1", "stop_name": "X", "stop_desc": "",
                    "nextride_url": "https://app.rtd-denver.com/nextride/stop/S1",
                },
            ],
            "error": "fetch failed",
        }
        assert build_tool_map_viewer_links(ARRIVALS_TOOL, output) == []

    def test_vehicles_emits_route_url_with_label(self):
        output = {
            "matched_route": {
                "short_name": "W", "long_name": "W Line",
                "nextride_url": "https://app.rtd-denver.com/nextride/route/W",
            },
        }
        links = build_tool_map_viewer_links(VEHICLES_TOOL, output)
        assert links == [
            {"url": "https://app.rtd-denver.com/nextride/route/W", "label": "W — W Line"}
        ]

    def test_vehicles_no_match_returns_empty(self):
        assert build_tool_map_viewer_links(VEHICLES_TOOL, {"matched_route": None}) == []
