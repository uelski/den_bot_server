"""Shared pytest fixtures for the Denver Open Data RAG test suite."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.documents import Document


def _make_doc(
    *,
    service_name: str,
    base_url: str = "https://example.arcgis.com/services/Example/FeatureServer",
    hub_url: str | None = None,
    has_layers: bool = False,
    doc_type: str | None = None,
    neighborhood_name: str | None = None,
    topic: str | None = None,
    page_content: str = "example content",
    **extra: Any,
) -> Document:
    metadata: dict[str, Any] = {
        "service_name": service_name,
        "base_url": base_url,
        "has_layers": has_layers,
    }
    if hub_url:
        metadata["hub_url"] = hub_url
    if doc_type:
        metadata["doc_type"] = doc_type
    if neighborhood_name:
        metadata["neighborhood_name"] = neighborhood_name
    if topic:
        metadata["topic"] = topic
    metadata.update(extra)
    return Document(page_content=page_content, metadata=metadata)


@pytest.fixture
def make_doc():
    """Factory for building Document objects with reasonable defaults."""
    return _make_doc


@pytest.fixture
def catalog_doc(make_doc):
    """A catalog-style retrieved Document (has_layers=True, no neighborhood metadata)."""
    return make_doc(
        service_name="Denver Parks",
        base_url="https://example.arcgis.com/services/Parks/FeatureServer",
        hub_url="https://opendata-geospatialdenver.hub.arcgis.com/datasets/parks/about",
        has_layers=True,
    )


@pytest.fixture
def kb_doc_factory():
    """Factory for PDF knowledge-base child Documents (flat KB payload shape).

    Mirrors what app/retrieval/kb.py produces: page_content is the child_text,
    parent_text + page ranges + provenance ride in metadata. base_url is
    intentionally absent — KB docs cite by document, not GIS service.
    """

    def _factory(
        *,
        document_id: str,
        parent_index: int = 0,
        child_index: int = 0,
        document_title: str = "Denver Code of Ordinances",
        source_url: str | None = "https://denvergov.org/code.pdf",
        category: str = "ordinance",
        parent_text: str = "full parent chunk text",
        child_text: str = "child chunk text",
        parent_start_page: int = 11,
        parent_end_page: int = 14,
    ) -> Document:
        return Document(
            page_content=child_text,
            metadata={
                "source_collection": "knowledge_base",
                "document_id": document_id,
                "document_title": document_title,
                "source_url": source_url,
                "category": category,
                "child_index": child_index,
                "child_text": child_text,
                "parent_index": parent_index,
                "parent_text": parent_text,
                "parent_start_page": parent_start_page,
                "parent_end_page": parent_end_page,
            },
        )

    return _factory


@pytest.fixture
def neighborhood_doc_factory(make_doc):
    """Factory for neighborhood_demographics-style retrieved Documents."""

    def _factory(neighborhood_name: str, topic: str = "population") -> Document:
        return make_doc(
            service_name="Denver Neighborhood Demographics (ACS 2017-2021)",
            base_url="https://example.arcgis.com/services/NBHD/FeatureServer",
            hub_url="https://opendata-geospatialdenver.hub.arcgis.com/datasets/nbhd/about",
            has_layers=False,
            doc_type="neighborhood_demographics",
            neighborhood_name=neighborhood_name,
            topic=topic,
            page_content=f"{neighborhood_name} — {topic}: mock body",
        )

    return _factory


@pytest.fixture
def mock_llm():
    """A MagicMock stand-in for a ChatGoogleGenerativeAI instance.

    Configure in tests like:
        mock_llm.with_structured_output.return_value = <chain>
        <chain>.invoke.return_value = <pydantic-model-or-dict>
    """
    return MagicMock()
