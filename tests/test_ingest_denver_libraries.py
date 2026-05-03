"""Tests for scripts/ingest_denver_libraries.

The tests exercise the pure functions (address formatter, activity sentence,
status sentence, page_content, build_library_document, build_documents) —
no Qdrant or network. Mirrors the test pattern in test_ingest_denver_parks.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_denver_libraries.py"


@pytest.fixture(scope="module")
def libs_module():
    spec = importlib.util.spec_from_file_location("ingest_denver_libraries", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_libraries"] = module
    spec.loader.exec_module(module)
    return module


def _props(**overrides) -> dict:
    """Build a properties dict with sensible defaults for any test case."""
    base = {
        "OBJECTID": 1,
        "LIBRARY_NAME": "Decker Branch Library",
        "BRANCH_CODE": "DEC",
        "GLOBALID": "abc-123",
        "ADDRESS_ID": 12345,
        "ADDRESS_LINE1": "1501 S Logan St",
        "ADDRESS_LINE2": None,
        "CITY": "Denver",
        "STATE": "CO",
        "ZIP": "80210",
        "DOOR_COUNT": 18234,
        "CIRCULATION": 12876,
        "TOTAL_BLDG_SQFT": 7500,
        "STATUS": None,
    }
    base.update(overrides)
    return base


def _feature(**prop_overrides) -> dict:
    return {
        "type": "Feature",
        "properties": _props(**prop_overrides),
        "geometry": {"type": "Point", "coordinates": [-104.9824, 39.7028]},
    }


# --- address formatter ------------------------------------------------------


class TestFormatAddress:
    def test_full_address(self, libs_module):
        s = libs_module.format_address(_props())
        assert s == "1501 S Logan St, Denver, CO 80210"

    def test_with_line2(self, libs_module):
        s = libs_module.format_address(_props(ADDRESS_LINE2="Suite B"))
        assert s == "1501 S Logan St, Suite B, Denver, CO 80210"

    def test_missing_line1_returns_none(self, libs_module):
        # Without a street address there's no useful line to render.
        assert libs_module.format_address(_props(ADDRESS_LINE1=None)) is None
        assert libs_module.format_address(_props(ADDRESS_LINE1="   ")) is None

    def test_missing_zip_omits_zip(self, libs_module):
        s = libs_module.format_address(_props(ZIP=""))
        assert s == "1501 S Logan St, Denver, CO"

    def test_missing_city_falls_back_to_denver(self, libs_module):
        s = libs_module.format_address(_props(CITY=None))
        assert "Denver, CO" in s

    def test_missing_state_falls_back_to_co(self, libs_module):
        s = libs_module.format_address(_props(STATE=None))
        assert "Denver, CO" in s


# --- activity sentence ------------------------------------------------------


class TestBuildActivitySentence:
    def test_all_three_combine(self, libs_module):
        s = libs_module.build_activity_sentence(_props())
        assert s == (
            "The 7,500-square-foot building recorded 12,876 circulation events "
            "and 18,234 door visits in the most recent reporting period."
        )

    def test_only_sqft_renders_size_only(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(CIRCULATION=0, DOOR_COUNT=0)
        )
        assert s == "The branch occupies a 7,500-square-foot building."

    def test_only_circulation_renders_activity_only(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(TOTAL_BLDG_SQFT=0, DOOR_COUNT=0)
        )
        assert s == (
            "The branch recorded 12,876 circulation events "
            "in the most recent reporting period."
        )

    def test_only_door_count_renders_activity_only(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(TOTAL_BLDG_SQFT=0, CIRCULATION=0)
        )
        assert s == (
            "The branch recorded 18,234 door visits "
            "in the most recent reporting period."
        )

    def test_circulation_and_door_without_sqft(self, libs_module):
        s = libs_module.build_activity_sentence(_props(TOTAL_BLDG_SQFT=None))
        assert s == (
            "The branch recorded 12,876 circulation events and 18,234 door visits "
            "in the most recent reporting period."
        )

    def test_all_zero_returns_none(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(TOTAL_BLDG_SQFT=0, CIRCULATION=0, DOOR_COUNT=0)
        )
        assert s is None

    def test_all_null_returns_none(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(TOTAL_BLDG_SQFT=None, CIRCULATION=None, DOOR_COUNT=None)
        )
        assert s is None

    def test_thousands_separator_applied(self, libs_module):
        s = libs_module.build_activity_sentence(
            _props(TOTAL_BLDG_SQFT=125000, CIRCULATION=1234567, DOOR_COUNT=98765)
        )
        assert "125,000" in s
        assert "1,234,567" in s
        assert "98,765" in s


# --- status sentence --------------------------------------------------------


class TestBuildStatusSentence:
    def test_returns_none_when_null(self, libs_module):
        assert libs_module.build_status_sentence(None) is None

    def test_returns_none_when_empty(self, libs_module):
        assert libs_module.build_status_sentence("") is None
        assert libs_module.build_status_sentence("   ") is None

    def test_emits_verbatim_with_status_prefix(self, libs_module):
        s = libs_module.build_status_sentence("Temporarily closed for renovations.")
        assert s == "Status: Temporarily closed for renovations."

    def test_truncated_source_text_emitted_anyway(self, libs_module):
        # The source data sometimes truncates these mid-word; we surface
        # them anyway because partial advisories are still user-relevant.
        s = libs_module.build_status_sentence(
            "The Blair-Caldwell African American Research Library is temporarily cl"
        )
        assert s.startswith("Status: ")
        assert "temporarily cl" in s


# --- page_content composition -----------------------------------------------


class TestBuildPageContent:
    def test_full_record_renders_all_sections(self, libs_module):
        text = libs_module.build_page_content(_props())
        assert text.startswith(
            "Decker Branch Library is a Denver Public Library branch "
            "located at 1501 S Logan St, Denver, CO 80210."
        )
        assert "7,500-square-foot building" in text
        assert "12,876 circulation events and 18,234 door visits" in text

    def test_omits_activity_sentence_when_no_data(self, libs_module):
        text = libs_module.build_page_content(
            _props(TOTAL_BLDG_SQFT=None, CIRCULATION=None, DOOR_COUNT=None)
        )
        assert "square-foot" not in text
        assert "circulation" not in text
        assert "door visits" not in text

    def test_includes_status_when_present(self, libs_module):
        text = libs_module.build_page_content(
            _props(STATUS="Temporarily closed.")
        )
        assert "Status: Temporarily closed." in text

    def test_falls_back_to_generic_lead_when_address_missing(self, libs_module):
        text = libs_module.build_page_content(_props(ADDRESS_LINE1=None))
        assert text.startswith(
            "Decker Branch Library is a Denver Public Library branch in Denver."
        )

    def test_falls_back_to_default_name_when_library_name_missing(self, libs_module):
        text = libs_module.build_page_content(_props(LIBRARY_NAME=None))
        assert text.startswith("Denver Library is a Denver Public Library branch")

    def test_strips_trailing_whitespace_in_name(self, libs_module):
        # The source data has trailing whitespace on at least one name.
        text = libs_module.build_page_content(
            _props(LIBRARY_NAME="John Emhoolah, Jr. ")
        )
        # No double spaces caused by the trailing whitespace.
        assert "John Emhoolah, Jr.  is" not in text
        assert text.startswith("John Emhoolah, Jr. is")


# --- document construction --------------------------------------------------


class TestBuildLibraryDocument:
    def test_returns_none_for_missing_geometry(self, libs_module):
        feature = {"type": "Feature", "properties": _props(), "geometry": None}
        assert libs_module.build_library_document(feature) is None

    def test_returns_none_for_missing_coordinates(self, libs_module):
        feature = {
            "type": "Feature", "properties": _props(),
            "geometry": {"type": "Point", "coordinates": []},
        }
        assert libs_module.build_library_document(feature) is None

    def test_returns_none_for_invalid_coordinates(self, libs_module):
        feature = {
            "type": "Feature", "properties": _props(),
            "geometry": {"type": "Point", "coordinates": ["bad", "data"]},
        }
        assert libs_module.build_library_document(feature) is None

    def test_doc_has_expected_metadata_keys(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        assert doc is not None
        for key in (
            "doc_type", "library_name", "branch_code",
            "address_line1", "city", "zip",
            "door_count", "circulation", "total_bldg_sqft", "status",
            "globalid", "objectid",
            "location", "service_name", "display_name",
            "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata",
        ):
            assert key in doc.metadata, f"missing metadata key {key!r}"

    def test_doc_type_and_service_name(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        assert doc.metadata["doc_type"] == "denver_library"
        assert doc.metadata["service_name"] == "Denver Libraries"

    def test_display_name_is_library_name(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        assert doc.metadata["display_name"] == "Decker Branch Library"

    def test_display_name_strips_trailing_whitespace(self, libs_module):
        doc = libs_module.build_library_document(
            _feature(LIBRARY_NAME="John Emhoolah, Jr. ")
        )
        assert doc.metadata["display_name"] == "John Emhoolah, Jr."

    def test_location_uses_geometry_coordinates(self, libs_module):
        # geojson coords are [lon, lat]; metadata.location is {lat, lon}.
        doc = libs_module.build_library_document(_feature())
        assert doc.metadata["location"] == {"lat": 39.7028, "lon": -104.9824}

    def test_map_url_is_per_library_google_maps(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        assert doc.metadata["map_url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.7028,-104.9824"
        )

    def test_base_url_and_hub_url_are_dataset_hub_constant(self, libs_module):
        # Two libraries at different coordinates share the same base/hub URL.
        d1 = libs_module.build_library_document(_feature())
        d2 = libs_module.build_library_document(
            _feature(geometry_coords=None)  # ignored; geometry built in _feature
        )
        # Re-create d2 with explicit different coords:
        d2 = libs_module.build_library_document({
            "type": "Feature",
            "properties": _props(LIBRARY_NAME="Other Branch"),
            "geometry": {"type": "Point", "coordinates": [-105.0, 39.5]},
        })
        assert d1.metadata["base_url"] == d2.metadata["base_url"]
        assert d1.metadata["hub_url"] == d2.metadata["hub_url"]
        assert d1.metadata["base_url"] == d1.metadata["hub_url"]
        assert "3d044de34a8049d9b90ed8e5720e3991" in d1.metadata["base_url"]

    def test_per_library_map_urls_differ(self, libs_module):
        d1 = libs_module.build_library_document(_feature())
        d2 = libs_module.build_library_document({
            "type": "Feature",
            "properties": _props(LIBRARY_NAME="Other"),
            "geometry": {"type": "Point", "coordinates": [-105.0, 39.5]},
        })
        assert d1.metadata["map_url"] != d2.metadata["map_url"]

    def test_full_metadata_round_trips_as_json(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        parsed = json.loads(doc.metadata["full_metadata"])
        assert parsed["library_name"] == "Decker Branch Library"
        assert parsed["branch_code"] == "DEC"
        assert parsed["circulation"] == 12876

    def test_has_layers_false(self, libs_module):
        doc = libs_module.build_library_document(_feature())
        assert doc.metadata["has_layers"] is False


# --- batch builder -----------------------------------------------------------


class TestBuildDocuments:
    def test_skips_features_without_geometry(self, libs_module):
        features = [
            _feature(),
            {"type": "Feature", "properties": _props(LIBRARY_NAME="No Geom"), "geometry": None},
            _feature(LIBRARY_NAME="Other"),
        ]
        docs = libs_module.build_documents(features)
        assert len(docs) == 2
        names = {d.metadata["library_name"] for d in docs}
        assert names == {"Decker Branch Library", "Other"}

    def test_empty_features_returns_empty(self, libs_module):
        assert libs_module.build_documents([]) == []
