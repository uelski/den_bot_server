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


class TestFormatDocsKnowledgeBase:
    def test_kb_doc_header_uses_title_and_page_range(self, kb_doc_factory):
        # By the generator stage the reranker has already expanded page_content
        # to the parent text; _format_docs just renders whatever page_content is.
        doc = kb_doc_factory(
            document_id="ord.pdf",
            document_title="Denver Code of Ordinances",
            parent_start_page=11,
            parent_end_page=14,
            child_text="the parent body the model reasoned over",
        )
        result = _format_docs([doc])
        assert result.startswith("[Denver Code of Ordinances, pages 11–14]")
        assert "the parent body the model reasoned over" in result

    def test_kb_doc_single_page_when_range_collapses(self, kb_doc_factory):
        doc = kb_doc_factory(
            document_id="x.pdf", parent_start_page=7, parent_end_page=7
        )
        result = _format_docs([doc])
        assert "page 7]" in result
        assert "pages" not in result

    def test_kb_doc_falls_back_to_filename_when_no_title(self, kb_doc_factory):
        doc = kb_doc_factory(document_id="x.pdf", document_title="")
        doc.metadata["original_filename"] = "budget2025.pdf"
        result = _format_docs([doc])
        assert "[budget2025.pdf" in result

    def test_scraped_page_cites_title_only(self, denvergov_page_doc_factory):
        """A scraped page always chunks to "page 1" — citing it would claim
        precision that doesn't exist, so pages cite by title alone."""
        doc = denvergov_page_doc_factory(
            document_title="City Budget",
            child_text="the budget page body",
        )
        result = _format_docs([doc])
        assert result.startswith("[City Budget]")
        assert "page" not in result.split("\n")[0]
        assert "the budget page body" in result

    def test_scraped_page_and_pdf_cite_differently(
        self, kb_doc_factory, denvergov_page_doc_factory
    ):
        docs = [
            kb_doc_factory(
                document_id="ord.pdf",
                document_title="Denver Code of Ordinances",
                parent_start_page=11,
                parent_end_page=14,
            ),
            denvergov_page_doc_factory(document_title="City Budget"),
        ]
        result = _format_docs(docs)
        assert "[Denver Code of Ordinances, pages 11–14]" in result
        assert "[City Budget]" in result


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
