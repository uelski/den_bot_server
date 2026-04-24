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
