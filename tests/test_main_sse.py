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


class TestSummarizeToolOutput:
    def test_none_output(self):
        assert _summarize_tool_output(None) == {"ok": False, "error": "no output"}

    def test_dict_with_error_field(self):
        result = _summarize_tool_output({"error": "no centroid"})
        assert result == {"ok": False, "error": "no centroid"}

    def test_weather_forecast_dict(self):
        forecast = {
            "neighborhood_name": "Five Points",
            "lat": 39.76,
            "lon": -104.97,
            "periods": [{"name": "Tonight"}, {"name": "Tomorrow"}],
        }
        result = _summarize_tool_output(forecast)
        assert result == {
            "ok": True,
            "neighborhood_name": "Five Points",
            "lat": 39.76,
            "lon": -104.97,
            "period_count": 2,
        }

    def test_dict_without_known_keys(self):
        assert _summarize_tool_output({"some": "payload"}) == {"ok": True}

    def test_skips_none_values(self):
        result = _summarize_tool_output({"neighborhood_name": None, "lat": None})
        assert result == {"ok": True}

    def test_non_dict_output(self):
        assert _summarize_tool_output("just a string") == {"ok": True}
