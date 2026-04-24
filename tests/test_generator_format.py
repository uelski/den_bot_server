"""Unit tests for _format_docs in the generator node.

This is the pure-function header-building logic that disambiguates catalog docs
from neighborhood demographics chunks in the LLM's context.
"""

from app.graph.nodes.generator import _format_docs


class TestFormatDocsCatalog:
    def test_catalog_doc_header_without_neighborhood(self, catalog_doc):
        result = _format_docs([catalog_doc])
        assert result.startswith("[Denver Parks]")
        # Hub page appended to same header line
        assert "(Hub page: https://opendata-geospatialdenver.hub.arcgis.com/datasets/parks/about)" in result
        assert "example content" in result

    def test_catalog_doc_without_hub_url_omits_hub_suffix(self, make_doc):
        doc = make_doc(service_name="Plain Dataset", base_url="https://x/fs")
        result = _format_docs([doc])
        assert result.startswith("[Plain Dataset]")
        assert "Hub page" not in result


class TestFormatDocsNeighborhood:
    def test_neighborhood_doc_header_includes_name_and_topic(
        self, neighborhood_doc_factory
    ):
        doc = neighborhood_doc_factory("Capitol Hill", "population")
        result = _format_docs([doc])
        # Pattern: [service — neighborhood (Topic)]
        assert "— Capitol Hill" in result
        assert "(Population)" in result

    def test_neighborhood_doc_titlecases_underscored_topic(
        self, neighborhood_doc_factory
    ):
        doc = neighborhood_doc_factory("Five Points", "income_poverty")
        result = _format_docs([doc])
        assert "(Income Poverty)" in result

    def test_mixed_docs_render_distinct_headers(
        self, catalog_doc, neighborhood_doc_factory
    ):
        nbhd = neighborhood_doc_factory("Globeville", "housing")
        result = _format_docs([catalog_doc, nbhd])
        parts = result.split("---")
        assert len(parts) == 2
        assert "[Denver Parks]" in parts[0]
        assert "— Globeville" in parts[1]
        assert "(Housing)" in parts[1]


class TestFormatDocsStructure:
    def test_docs_separated_by_hr(self, catalog_doc, make_doc):
        second = make_doc(
            service_name="Other",
            base_url="https://other/fs",
            page_content="second body",
        )
        result = _format_docs([catalog_doc, second])
        assert "---" in result

    def test_empty_list_returns_empty_string(self):
        assert _format_docs([]) == ""
