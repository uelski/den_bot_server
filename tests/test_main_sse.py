"""Unit tests for the SSE payload builders in app.main."""

from app.main import (
    _summarize_tool_output,
    build_map_viewer_links,
    build_sources_payload,
    build_tool_map_viewer_links,
    build_tool_sources,
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

    def test_multiple_park_docs_collapse_to_one_source(self, make_doc):
        # Parks share service_name + base_url + hub_url across all docs; the
        # per-park uniqueness lives on map_url/display_name and only matters
        # for map_viewer. Sources should dedup to a single citation entry.
        docs = [
            make_doc(
                service_name="Denver Parks",
                base_url="https://hub.example/parks",
                hub_url="https://hub.example/parks",
                map_url=f"https://maps.example/{i}",
                display_name=f"Park {i}",
                doc_type="denver_park",
            )
            for i in range(5)
        ]
        result = build_sources_payload(docs)
        assert len(result) == 1
        assert result[0]["service_name"] == "Denver Parks"

    def test_kb_doc_emits_document_citation(self, kb_doc_factory):
        doc = kb_doc_factory(
            document_id="ord.pdf",
            document_title="Denver Code of Ordinances",
            source_url="https://denvergov.org/code.pdf",
            parent_start_page=11,
            parent_end_page=14,
            category="ordinance",
        )
        result = build_sources_payload([doc])
        assert len(result) == 1
        entry = result[0]
        assert entry["source_collection"] == "knowledge_base"
        # document_id lets the frontend offer an in-chat download via the
        # /knowledge-base/documents/download endpoint.
        assert entry["document_id"] == "ord.pdf"
        assert entry["document_title"] == "Denver Code of Ordinances"
        assert entry["source_url"] == "https://denvergov.org/code.pdf"
        assert entry["page_start"] == 11 and entry["page_end"] == 14
        assert entry["category"] == "ordinance"
        # KB docs carry no base_url; must not be dropped by the catalog path.
        assert "base_url" not in entry

    def test_kb_chunks_from_same_document_dedup_by_document_id(self, kb_doc_factory):
        docs = [
            kb_doc_factory(document_id="ord.pdf", parent_index=i, child_index=i)
            for i in range(4)
        ]
        result = build_sources_payload(docs)
        assert len(result) == 1

    def test_mixed_catalog_and_kb_both_emit(self, catalog_doc, kb_doc_factory):
        docs = [catalog_doc, kb_doc_factory(document_id="ord.pdf")]
        result = build_sources_payload(docs)
        assert len(result) == 2
        kinds = {e.get("source_collection") for e in result}
        assert "knowledge_base" in kinds


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

    def test_map_url_preferred_over_hub_url(self, make_doc):
        doc = make_doc(
            service_name="Denver Parks",
            base_url="https://hub.example/dataset",
            hub_url="https://hub.example/dataset",
            map_url="https://www.google.com/maps/search/?api=1&query=39.73,-104.96",
        )
        result = build_map_viewer_links([doc])
        assert len(result) == 1
        assert result[0]["url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.73,-104.96"
        )

    def test_display_name_preferred_over_service_name(self, make_doc):
        doc = make_doc(
            service_name="Denver Parks",
            base_url="https://hub.example/dataset",
            hub_url="https://hub.example/dataset",
            map_url="https://maps.example/city-park",
            display_name="City Park",
        )
        result = build_map_viewer_links([doc])
        assert result[0]["label"] == "View City Park map"

    def test_unique_map_urls_emit_separate_entries_with_unique_labels(self, make_doc):
        # Mirrors the parks/RTD shape: shared dataset hub_url + service_name,
        # unique per-entity map_url + display_name. All entries should surface
        # with distinct labels.
        docs = [
            make_doc(
                service_name="Denver Parks",
                base_url="https://hub.example/parks",
                hub_url="https://hub.example/parks",
                map_url=f"https://maps.example/{slug}",
                display_name=name,
            )
            for slug, name in [("city-park", "City Park"), ("cheesman", "Cheesman Park")]
        ]
        result = build_map_viewer_links(docs)
        assert len(result) == 2
        assert {r["label"] for r in result} == {
            "View City Park map", "View Cheesman Park map",
        }

    def test_shared_hub_url_with_unique_map_urls_does_not_dedup(self, make_doc):
        # The fix's whole point: 5 RTD stops with the same hub_url collapse to
        # ONE source entry but produce FIVE map_viewer entries (deduped by
        # map_url, not hub_url).
        docs = [
            make_doc(
                service_name="RTD Transit (GTFS)",
                base_url="https://app.rtd-denver.com/",
                hub_url="https://app.rtd-denver.com/",
                map_url=f"https://app.rtd-denver.com/nextride/stop/S{i}",
                display_name=f"Stop {i}",
            )
            for i in range(5)
        ]
        result = build_map_viewer_links(docs)
        assert len(result) == 5


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


DENVERGOV_TOOL = "search_denver_gov"
DENVERGOV_KEYS = {"ok", "error", "query", "result_count", "sample"}


class TestSummarizeDenvergovSearch:
    """The denvergov.org Tavily-backed search tool. Same fixed-keyset
    discriminated-union convention as the other tools."""

    def test_full_keyset_populated_on_success(self):
        output = {
            "query": "how do I pay a parking ticket",
            "results": [
                {
                    "title": "Pay a parking ticket",
                    "url": "https://www.denvergov.org/pay-ticket",
                    "snippet": "Pay your Denver parking ticket online...",
                    "score": 0.92,
                },
                {
                    "title": "Parking enforcement",
                    "url": "https://www.denvergov.org/parking-enforcement",
                    "snippet": "Information about parking enforcement.",
                    "score": 0.84,
                },
            ],
        }
        result = _summarize_tool_output(DENVERGOV_TOOL, output)
        assert set(result.keys()) == DENVERGOV_KEYS
        assert result["ok"] is True
        assert result["error"] is None
        assert result["query"] == "how do I pay a parking ticket"
        assert result["result_count"] == 2
        assert len(result["sample"]) == 2
        assert result["sample"][0]["title"] == "Pay a parking ticket"
        assert result["sample"][0]["url"].startswith("https://www.denvergov.org/")

    def test_error_keeps_full_keyset_with_nulls(self):
        result = _summarize_tool_output(DENVERGOV_TOOL, {"error": "Tavily down"})
        assert set(result.keys()) == DENVERGOV_KEYS
        assert result["ok"] is False
        assert result["error"] == "Tavily down"
        assert result["query"] is None
        assert result["result_count"] is None
        assert result["sample"] is None

    def test_empty_results_render_empty_sample(self):
        output = {"query": "nothing matches", "results": []}
        result = _summarize_tool_output(DENVERGOV_TOOL, output)
        assert result["ok"] is True
        assert result["query"] == "nothing matches"
        assert result["result_count"] == 0
        assert result["sample"] == []

    def test_sample_truncates_long_snippets(self):
        output = {
            "query": "q",
            "results": [
                {
                    "title": "T",
                    "url": "https://www.denvergov.org/t",
                    "snippet": "x" * 500,
                },
            ],
        }
        result = _summarize_tool_output(DENVERGOV_TOOL, output)
        # Sample snippet is capped at 200 chars by _extract_denvergov_search_fields,
        # independent of the tool-level 300-char SNIPPET_MAX_CHARS cap.
        assert len(result["sample"][0]["snippet"]) == 200

    def test_sample_caps_at_two(self):
        output = {
            "query": "q",
            "results": [
                {
                    "title": f"Title {i}",
                    "url": f"https://www.denvergov.org/{i}",
                    "snippet": f"Snippet {i}",
                }
                for i in range(5)
            ],
        }
        result = _summarize_tool_output(DENVERGOV_TOOL, output)
        assert result["result_count"] == 5
        assert len(result["sample"]) == 2
        assert result["sample"][0]["title"] == "Title 0"
        assert result["sample"][1]["title"] == "Title 1"


class TestBuildToolSourcesDenvergov:
    """Tavily's URLs feed the `sources` SSE event (citation-shaped) rather
    than the `map_viewer` panel — these aren't maps. Each hit becomes one
    source entry with the page title as service_name (the label) and the
    URL as both base_url and hub_url (frontend uses hub_url as the link)."""

    def test_emits_one_source_per_result_with_titles_as_service_name(self):
        output = {
            "query": "q",
            "results": [
                {"title": "Pay a parking ticket", "url": "https://www.denvergov.org/pay-ticket", "snippet": ""},
                {"title": "Parking enforcement", "url": "https://www.denvergov.org/parking-enforcement", "snippet": ""},
            ],
        }
        sources = build_tool_sources(DENVERGOV_TOOL, output)
        assert len(sources) == 2
        assert sources[0] == {
            "service_name": "Pay a parking ticket",
            "base_url": "https://www.denvergov.org/pay-ticket",
            "hub_url": "https://www.denvergov.org/pay-ticket",
            "doc_type": "denvergov_search_result",
        }
        assert sources[1]["service_name"] == "Parking enforcement"

    def test_every_entry_carries_doc_type_discriminator(self):
        # The discriminator lets the frontend render Tavily-derived links
        # differently from retrieval-driven catalog sources (which carry
        # doc_types like denver_park, neighborhood_demographics, etc.).
        output = {
            "query": "q",
            "results": [
                {"title": f"Page {i}", "url": f"https://www.denvergov.org/p{i}", "snippet": ""}
                for i in range(3)
            ],
        }
        sources = build_tool_sources(DENVERGOV_TOOL, output)
        assert len(sources) == 3
        for entry in sources:
            assert entry["doc_type"] == "denvergov_search_result"

    def test_skips_when_error(self):
        output = {
            "query": "q",
            "results": [
                {"title": "T", "url": "https://www.denvergov.org/t", "snippet": ""},
            ],
            "error": "Tavily down",
        }
        assert build_tool_sources(DENVERGOV_TOOL, output) == []

    def test_skips_hits_missing_url(self):
        output = {
            "query": "q",
            "results": [
                {"title": "Has URL", "url": "https://www.denvergov.org/a", "snippet": ""},
                {"title": "Missing URL", "url": "", "snippet": ""},
            ],
        }
        sources = build_tool_sources(DENVERGOV_TOOL, output)
        assert len(sources) == 1
        assert sources[0]["hub_url"] == "https://www.denvergov.org/a"

    def test_dedupes_by_service_name_base_url_hub_url(self):
        same = {"title": "T", "url": "https://www.denvergov.org/x", "snippet": ""}
        output = {"query": "q", "results": [same, same, same]}
        assert len(build_tool_sources(DENVERGOV_TOOL, output)) == 1

    def test_falls_back_to_default_label_when_title_empty(self):
        output = {
            "query": "q",
            "results": [
                {"title": "", "url": "https://www.denvergov.org/x", "snippet": ""},
                {"title": "   ", "url": "https://www.denvergov.org/y", "snippet": ""},
            ],
        }
        sources = build_tool_sources(DENVERGOV_TOOL, output)
        # Both fall back to the default label, but distinct URLs keep them
        # as separate entries.
        assert sources[0]["service_name"] == "denvergov.org page"
        assert sources[1]["service_name"] == "denvergov.org page"
        assert sources[0]["hub_url"] != sources[1]["hub_url"]

    def test_no_results_returns_empty(self):
        assert build_tool_sources(DENVERGOV_TOOL, {"query": "q", "results": []}) == []

    def test_unknown_tool_returns_empty(self):
        assert build_tool_sources("not_a_real_tool", {"results": []}) == []

    def test_search_denver_gov_no_longer_emits_map_viewer_links(self):
        # Regression guard: this tool used to emit map_viewer entries, but
        # was moved to sources because the URLs aren't maps.
        output = {
            "query": "q",
            "results": [
                {"title": "T", "url": "https://www.denvergov.org/t", "snippet": ""},
            ],
        }
        assert build_tool_map_viewer_links(DENVERGOV_TOOL, output) == []
