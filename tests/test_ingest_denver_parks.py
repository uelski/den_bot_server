"""Tests for scripts/ingest_denver_parks.

The tests exercise the pure functions (descriptor, locator, amenities,
page_content, build_park_document, build_documents) — no Qdrant or
network. Mirrors the test pattern in test_ingest_rtd_gtfs.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load the ingest script as a module without requiring scripts/ to be a package.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_denver_parks.py"


@pytest.fixture(scope="module")
def parks_module():
    spec = importlib.util.spec_from_file_location("ingest_denver_parks", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_parks"] = module
    spec.loader.exec_module(module)
    return module


def _props(**overrides) -> dict:
    """Build a properties dict with sensible defaults for any test case."""
    base = {
        "FORMAL_NAME": "Cheesman Park",
        "LOCATION": "Cheesman",
        "PARK_TYPE": "Park",
        "PARK_CLASS": "Community",
        "GIS_ACRES": 80.7,
        "ADDRESS_LINE1": "1599 E 8th Ave",
        "CROSS_STREETS": "8th Ave & Franklin St",
        "FACILITIES": "Pavilion, Playground, Tennis Court",
        "LATITUDE": 39.7339,
        "LONGITUDE": -104.9631,
        "OBJECTID": 42,
        "GLOBALID": "abc-123",
        "COUNCIL_DIST": "10",
        "MAINT_DIST": "MP",
        "DESIGNATED": "Yes - 1956",
    }
    base.update(overrides)
    return base


def _feature(**prop_overrides) -> dict:
    return {"type": "Feature", "properties": _props(**prop_overrides),
            "geometry": {"type": "Polygon", "coordinates": [[]]}}


# --- descriptor rule ---------------------------------------------------------


class TestDescriptor:
    def test_park_type_uses_class_with_park_suffix(self, parks_module):
        assert parks_module.build_descriptor("Park", "Neighborhood") == "neighborhood park"
        assert parks_module.build_descriptor("Park", "Mountain") == "mountain park"
        assert parks_module.build_descriptor("Park", "Linear") == "linear park"

    def test_grounds_uses_class_only(self, parks_module):
        # Type=Grounds, Class=Recreation Center -> "recreation center"
        assert parks_module.build_descriptor("Grounds", "Recreation Center") == "recreation center"

    def test_golf_uses_class_only(self, parks_module):
        assert parks_module.build_descriptor("Golf", "Regional") == "regional"

    def test_facility_uses_class_only(self, parks_module):
        assert parks_module.build_descriptor("Facility", "Special Use") == "special use"

    def test_open_space_avoids_stutter(self, parks_module):
        assert parks_module.build_descriptor("Open Space", "Open Space") == "open space"

    def test_distinct_type_class_are_combined(self, parks_module):
        assert (
            parks_module.build_descriptor("Conservation Tract", "Pocket")
            == "pocket conservation tract"
        )

    def test_empty_inputs_default_to_park(self, parks_module):
        assert parks_module.build_descriptor("", "") == "park"
        assert parks_module.build_descriptor(None, None) == "park"


# --- locator + amenities sentences ------------------------------------------


class TestLocatorSentence:
    def test_address_and_cross_combine(self, parks_module):
        s = parks_module.build_locator_sentence("100 Main St", "Main & 1st")
        assert s == "Located at 100 Main St, near Main & 1st."

    def test_address_only(self, parks_module):
        s = parks_module.build_locator_sentence("100 Main St", None)
        assert s == "Located at 100 Main St."

    def test_cross_streets_only(self, parks_module):
        s = parks_module.build_locator_sentence(None, "Main & 1st")
        assert s == "Located near Main & 1st."

    def test_neither_returns_none(self, parks_module):
        assert parks_module.build_locator_sentence(None, None) is None
        assert parks_module.build_locator_sentence("", "  ") is None


class TestAmenitiesSentence:
    def test_passes_facilities_verbatim(self, parks_module):
        s = parks_module.build_amenities_sentence("Dog Park, Natural Areas")
        assert s == "Amenities include: Dog Park, Natural Areas."

    def test_empty_returns_none(self, parks_module):
        assert parks_module.build_amenities_sentence(None) is None
        assert parks_module.build_amenities_sentence("") is None
        assert parks_module.build_amenities_sentence("   ") is None


# --- page_content composition -----------------------------------------------


class TestPageContent:
    def test_full_record_renders_all_sentences(self, parks_module):
        text = parks_module.build_page_content(_props())
        assert text.startswith("Cheesman Park is a 80.7-acre community park in Denver.")
        assert "Located at 1599 E 8th Ave, near 8th Ave & Franklin St." in text
        assert "Amenities include: Pavilion, Playground, Tennis Court." in text

    def test_missing_address_falls_back_to_cross_streets(self, parks_module):
        text = parks_module.build_page_content(_props(ADDRESS_LINE1=None))
        assert "Located near 8th Ave & Franklin St." in text
        assert "Located at" not in text

    def test_missing_both_locator_fields_omits_locator_sentence(self, parks_module):
        text = parks_module.build_page_content(
            _props(ADDRESS_LINE1=None, CROSS_STREETS=None)
        )
        assert "Located" not in text
        # Lead and amenities sentences still present
        assert text.startswith("Cheesman Park is a 80.7-acre community park")
        assert "Amenities include:" in text

    def test_missing_facilities_omits_amenities_sentence(self, parks_module):
        text = parks_module.build_page_content(_props(FACILITIES=None))
        assert "Amenities include" not in text

    def test_falls_back_to_location_when_formal_name_missing(self, parks_module):
        text = parks_module.build_page_content(_props(FORMAL_NAME=None))
        assert text.startswith("Cheesman is a 80.7-acre community park in Denver.")

    def test_acres_missing_skips_acreage_phrase(self, parks_module):
        text = parks_module.build_page_content(_props(GIS_ACRES=None))
        # "Cheesman Park is a community park in Denver." (no acreage)
        assert text.startswith("Cheesman Park is a community park in Denver.")
        assert "acre" not in text.split("Located")[0]

    def test_grounds_type_descriptor(self, parks_module):
        text = parks_module.build_page_content(
            _props(PARK_TYPE="Grounds", PARK_CLASS="Recreation Center")
        )
        assert "is a 80.7-acre recreation center in Denver" in text


# --- document construction ---------------------------------------------------


class TestBuildParkDocument:
    def test_returns_none_for_missing_coords(self, parks_module):
        feature = _feature(LATITUDE=None, LONGITUDE=None)
        assert parks_module.build_park_document(feature) is None

    def test_returns_none_for_invalid_coords(self, parks_module):
        feature = _feature(LATITUDE="not a number", LONGITUDE=-104.0)
        assert parks_module.build_park_document(feature) is None

    def test_doc_has_expected_metadata_keys(self, parks_module):
        feature = _feature()
        doc = parks_module.build_park_document(feature)
        assert doc is not None
        for key in (
            "doc_type", "park_name", "formal_name", "location_name",
            "park_type", "park_class", "acres",
            "address_line1", "cross_streets",
            "council_district", "maint_district",
            "globalid", "objectid",
            "location", "service_name",
            "base_url", "hub_url",
            "has_layers", "full_metadata",
        ):
            assert key in doc.metadata, f"missing metadata key {key!r}"

    def test_doc_type_is_denver_park(self, parks_module):
        doc = parks_module.build_park_document(_feature())
        assert doc.metadata["doc_type"] == "denver_park"
        assert doc.metadata["service_name"] == "Denver Parks"

    def test_location_uses_lat_lon_directly(self, parks_module):
        doc = parks_module.build_park_document(_feature())
        assert doc.metadata["location"] == {"lat": 39.7339, "lon": -104.9631}

    def test_hub_url_is_per_park_google_maps(self, parks_module):
        doc = parks_module.build_park_document(_feature())
        assert doc.metadata["hub_url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.7339,-104.9631"
        )

    def test_base_url_is_dataset_hub_constant(self, parks_module):
        # Two parks at different coordinates share the same base_url
        doc1 = parks_module.build_park_document(_feature())
        doc2 = parks_module.build_park_document(_feature(LATITUDE=39.5, LONGITUDE=-105.0))
        assert doc1.metadata["base_url"] == doc2.metadata["base_url"]
        assert "opendata-geospatialdenver.hub.arcgis.com" in doc1.metadata["base_url"]
        assert "e752556524024fbfac700b35f33c0f2c" in doc1.metadata["base_url"]

    def test_per_park_hub_urls_differ(self, parks_module):
        doc1 = parks_module.build_park_document(_feature(LATITUDE=39.7, LONGITUDE=-104.9))
        doc2 = parks_module.build_park_document(_feature(LATITUDE=39.5, LONGITUDE=-105.0))
        assert doc1.metadata["hub_url"] != doc2.metadata["hub_url"]

    def test_full_metadata_is_json_string(self, parks_module):
        doc = parks_module.build_park_document(_feature())
        parsed = json.loads(doc.metadata["full_metadata"])
        assert parsed["formal_name"] == "Cheesman Park"
        assert parsed["facilities"] == "Pavilion, Playground, Tennis Court"

    def test_has_layers_false(self, parks_module):
        doc = parks_module.build_park_document(_feature())
        assert doc.metadata["has_layers"] is False


# --- batch builder -----------------------------------------------------------


class TestBuildDocuments:
    def test_skips_features_without_coords(self, parks_module):
        features = [
            _feature(),
            _feature(FORMAL_NAME="No Coords", LATITUDE=None, LONGITUDE=None),
            _feature(FORMAL_NAME="Other Park", LATITUDE=39.5, LONGITUDE=-105.0),
        ]
        docs = parks_module.build_documents(features)
        assert len(docs) == 2
        names = {d.metadata["formal_name"] for d in docs}
        assert names == {"Cheesman Park", "Other Park"}

    def test_empty_features_returns_empty(self, parks_module):
        assert parks_module.build_documents([]) == []
