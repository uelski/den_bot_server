"""Tests for scripts/ingest_denver_rec_centers.

The tests exercise the pure functions (cleaner, address formatter, lead/phone/
building/facilities/programs sentence builders, page_content composition,
build_rec_center_document, build_documents) — no Qdrant or network. Mirrors
the test pattern in test_ingest_denver_libraries.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_denver_rec_centers.py"


@pytest.fixture(scope="module")
def rc_module():
    spec = importlib.util.spec_from_file_location("ingest_denver_rec_centers", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_rec_centers"] = module
    spec.loader.exec_module(module)
    return module


def _props(**overrides) -> dict:
    """Build a properties dict with sensible defaults for any test case."""
    base = {
        "OBJECTID": 1,
        "REC_NAME": "Scheitler Recreation Center",
        "LOC_CODE": "RB2",
        "ADDRESS_ID": None,
        "ADDRESS_LINE1": "5031 W 46th Ave",
        "ADDRESS_LINE2": None,
        "CITY": "Denver",
        "STATE": "CO",
        "ZIP": None,
        "PHONE": "720-865-0640",
        "REC_TYPE": "Regional",
        "MARKETING_CODE": "RF",
        "MARKETED_FACILITIES": "Dance Space, Gym (Small), Lockers, Meeting Room, Pool (Indoor)",
        "MARKETED_PROGRAMS": "Aquatics, Arts & Culture, Fitness & Health",
        "PROGRAMS_LINK": "documents/northwest.pdf",
        "POOL_HOURS_LINK": None,
        "NEWS_LINK": "www.denvergov.org/...",
        "HOURS": "documents/Recreation/Regional_Hours.pdf",
        "PHOTO": "images/recctrs/scheitlerrc.jpg",
        "YEAR_BUILT": "1972",
        "YEAR_REMODEL": "1976, 1981, 1992",
        "BLDG_SQFT": 26801,
        "LABEL": "Scheitler",
        "LATITUDE": 39.78138589,
        "LONGITUDE": -105.05123738,
        "FACILITIES_OLD": "Senior Center,Indoor Pool",
        "GLOBALID": "abc-123",
    }
    base.update(overrides)
    return base


def _feature(**prop_overrides) -> dict:
    return {
        "type": "Feature",
        "properties": _props(**prop_overrides),
        "geometry": {"type": "Point", "coordinates": [-105.05123738, 39.78138589]},
    }


# --- _clean -----------------------------------------------------------------


class TestClean:
    def test_returns_none_for_none(self, rc_module):
        assert rc_module._clean(None) is None

    def test_returns_none_for_empty_string(self, rc_module):
        assert rc_module._clean("") is None
        assert rc_module._clean("   ") is None

    def test_returns_none_for_literal_null_sentinel(self, rc_module):
        # The source data has the literal string "<Null>" in some fields.
        assert rc_module._clean("<Null>") is None

    def test_strips_whitespace(self, rc_module):
        assert rc_module._clean("  hello  ") == "hello"

    def test_returns_value_unchanged_when_normal(self, rc_module):
        assert rc_module._clean("Denver") == "Denver"

    def test_coerces_non_string_values(self, rc_module):
        assert rc_module._clean(42) == "42"


# --- format_address ---------------------------------------------------------


class TestFormatAddress:
    def test_full_address_omits_zip(self, rc_module):
        # ZIP is never populated in the source data; never rendered.
        s = rc_module.format_address(_props())
        assert s == "5031 W 46th Ave, Denver, CO"

    def test_with_line2(self, rc_module):
        s = rc_module.format_address(_props(ADDRESS_LINE2="Suite B"))
        assert s == "5031 W 46th Ave, Suite B, Denver, CO"

    def test_literal_null_in_line2_treated_as_missing(self, rc_module):
        s = rc_module.format_address(_props(ADDRESS_LINE2="<Null>"))
        assert s == "5031 W 46th Ave, Denver, CO"

    def test_missing_line1_returns_none(self, rc_module):
        assert rc_module.format_address(_props(ADDRESS_LINE1=None)) is None
        assert rc_module.format_address(_props(ADDRESS_LINE1="<Null>")) is None

    def test_missing_city_falls_back_to_denver(self, rc_module):
        s = rc_module.format_address(_props(CITY=None))
        assert "Denver, CO" in s

    def test_missing_state_falls_back_to_co(self, rc_module):
        s = rc_module.format_address(_props(STATE=None))
        assert "Denver, CO" in s


# --- lead sentence ----------------------------------------------------------


class TestBuildLeadSentence:
    def test_full_lead_with_type_and_address(self, rc_module):
        s = rc_module.build_lead_sentence(_props())
        assert s == (
            "Scheitler Recreation Center is a regional recreation center "
            "operated by Denver Parks & Recreation, "
            "located at 5031 W 46th Ave, Denver, CO."
        )

    def test_rec_type_lowercased(self, rc_module):
        # All three values render lowercased.
        for raw, lower in [("Regional", "regional"), ("Neighborhood", "neighborhood"), ("Local", "local")]:
            s = rc_module.build_lead_sentence(_props(REC_TYPE=raw))
            assert f"a {lower} recreation center" in s

    def test_missing_rec_type_falls_back_to_generic(self, rc_module):
        s = rc_module.build_lead_sentence(_props(REC_TYPE=None))
        assert "is a recreation center operated by" in s

    def test_missing_address_uses_generic_in_denver_phrasing(self, rc_module):
        s = rc_module.build_lead_sentence(_props(ADDRESS_LINE1=None))
        assert s.endswith("operated by Denver Parks & Recreation in Denver.")

    def test_missing_name_falls_back_to_default(self, rc_module):
        s = rc_module.build_lead_sentence(_props(REC_NAME=None))
        assert s.startswith("Recreation Center is a regional recreation center")


# --- phone sentence ---------------------------------------------------------


class TestBuildPhoneSentence:
    def test_emits_phone(self, rc_module):
        assert rc_module.build_phone_sentence(_props()) == "Phone: 720-865-0640."

    def test_returns_none_when_missing(self, rc_module):
        assert rc_module.build_phone_sentence(_props(PHONE=None)) is None
        assert rc_module.build_phone_sentence(_props(PHONE="")) is None
        assert rc_module.build_phone_sentence(_props(PHONE="<Null>")) is None


# --- building sentence ------------------------------------------------------


class TestBuildBuildingSentence:
    def test_full_with_remodel(self, rc_module):
        s = rc_module.build_building_sentence(_props())
        assert s == (
            "The 26,801-square-foot building was built in 1972 "
            "and remodeled in 1976, 1981, 1992."
        )

    def test_unknown_remodel_omitted(self, rc_module):
        s = rc_module.build_building_sentence(_props(YEAR_REMODEL="Unknown"))
        assert s == "The 26,801-square-foot building was built in 1972."

    def test_unknown_remodel_case_insensitive(self, rc_module):
        s = rc_module.build_building_sentence(_props(YEAR_REMODEL="unknown"))
        assert "remodeled" not in s

    def test_missing_remodel_omitted(self, rc_module):
        s = rc_module.build_building_sentence(_props(YEAR_REMODEL=None))
        assert "remodeled" not in s

    def test_only_sqft_no_year_built(self, rc_module):
        s = rc_module.build_building_sentence(
            _props(YEAR_BUILT=None, YEAR_REMODEL=None)
        )
        assert s == "The building is 26,801 square feet."

    def test_only_year_built_no_sqft(self, rc_module):
        s = rc_module.build_building_sentence(
            _props(BLDG_SQFT=None, YEAR_REMODEL=None)
        )
        assert s == "The building was built in 1972."

    def test_year_built_with_remodel_no_sqft(self, rc_module):
        s = rc_module.build_building_sentence(
            _props(BLDG_SQFT=None, YEAR_REMODEL="1985")
        )
        assert s == "The building was built in 1972 and remodeled in 1985."

    def test_neither_returns_none(self, rc_module):
        assert rc_module.build_building_sentence(
            _props(BLDG_SQFT=None, YEAR_BUILT=None)
        ) is None

    def test_invalid_sqft_treated_as_missing(self, rc_module):
        s = rc_module.build_building_sentence(
            _props(BLDG_SQFT="not a number", YEAR_REMODEL=None)
        )
        # Falls through to year_built only path.
        assert s == "The building was built in 1972."

    def test_thousands_separator(self, rc_module):
        s = rc_module.build_building_sentence(_props(BLDG_SQFT=125000))
        assert "125,000-square-foot" in s


# --- facilities + programs sentences ---------------------------------------


class TestBuildFacilitiesSentence:
    def test_passes_through_verbatim(self, rc_module):
        s = rc_module.build_facilities_sentence("Dance Space, Gym, Pool (Indoor)")
        assert s == "Marketed facilities include: Dance Space, Gym, Pool (Indoor)."

    def test_returns_none_when_empty(self, rc_module):
        assert rc_module.build_facilities_sentence(None) is None
        assert rc_module.build_facilities_sentence("") is None
        assert rc_module.build_facilities_sentence("   ") is None
        assert rc_module.build_facilities_sentence("<Null>") is None


class TestBuildProgramsSentence:
    def test_passes_through_verbatim(self, rc_module):
        s = rc_module.build_programs_sentence("Aquatics, Arts & Culture")
        assert s == "Programs offered: Aquatics, Arts & Culture."

    def test_returns_none_when_empty(self, rc_module):
        assert rc_module.build_programs_sentence(None) is None
        assert rc_module.build_programs_sentence("<Null>") is None


# --- page_content composition -----------------------------------------------


class TestBuildPageContent:
    def test_full_record_renders_all_sections(self, rc_module):
        text = rc_module.build_page_content(_props())
        assert "Scheitler Recreation Center is a regional recreation center" in text
        assert "located at 5031 W 46th Ave, Denver, CO" in text
        assert "Phone: 720-865-0640." in text
        assert "26,801-square-foot building was built in 1972 and remodeled in 1976, 1981, 1992." in text
        assert "Marketed facilities include: Dance Space" in text
        assert "Programs offered: Aquatics" in text

    def test_missing_optional_fields_omits_those_sentences(self, rc_module):
        text = rc_module.build_page_content(_props(
            PHONE=None, BLDG_SQFT=None, YEAR_BUILT=None,
            MARKETED_FACILITIES=None, MARKETED_PROGRAMS=None,
        ))
        assert "Phone:" not in text
        assert "square-foot" not in text
        assert "Marketed facilities" not in text
        assert "Programs offered" not in text
        # Lead sentence still present.
        assert "Scheitler Recreation Center" in text

    def test_hours_field_not_in_embedded_text(self, rc_module):
        # HOURS is a relative PDF path, not actual hours; deliberately omitted.
        text = rc_module.build_page_content(_props())
        assert "documents/Recreation/Regional_Hours.pdf" not in text
        assert "HOURS" not in text


# --- document construction --------------------------------------------------


class TestBuildRecCenterDocument:
    def test_returns_none_for_missing_lat_lon(self, rc_module):
        feature = _feature(LATITUDE=None, LONGITUDE=None)
        assert rc_module.build_rec_center_document(feature) is None

    def test_returns_none_for_invalid_lat_lon(self, rc_module):
        feature = _feature(LATITUDE="not a number", LONGITUDE=-105.0)
        assert rc_module.build_rec_center_document(feature) is None

    def test_doc_has_expected_metadata_keys(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc is not None
        for key in (
            "doc_type", "rec_name", "loc_code", "rec_type",
            "address_line1", "city", "phone", "year_built", "bldg_sqft",
            "globalid", "objectid",
            "location", "service_name", "display_name",
            "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata",
        ):
            assert key in doc.metadata, f"missing metadata key {key!r}"

    def test_doc_type_and_service_name(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc.metadata["doc_type"] == "denver_rec_center"
        assert doc.metadata["service_name"] == "Denver Recreation Centers"

    def test_display_name_is_rec_name(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc.metadata["display_name"] == "Scheitler Recreation Center"

    def test_location_uses_lat_lon_directly(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc.metadata["location"] == {"lat": 39.78138589, "lon": -105.05123738}

    def test_map_url_is_per_rec_center_google_maps(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc.metadata["map_url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.78138589,-105.05123738"
        )

    def test_base_url_and_hub_url_match_dataset_constant(self, rc_module):
        d1 = rc_module.build_rec_center_document(_feature())
        d2 = rc_module.build_rec_center_document(
            _feature(REC_NAME="Other", LATITUDE=39.5, LONGITUDE=-105.0)
        )
        assert d1.metadata["base_url"] == d2.metadata["base_url"]
        assert d1.metadata["hub_url"] == d2.metadata["hub_url"]
        assert d1.metadata["base_url"] == d1.metadata["hub_url"]
        assert "geospatialDenver::recreation-centers" in d1.metadata["base_url"]

    def test_per_rec_center_map_urls_differ(self, rc_module):
        d1 = rc_module.build_rec_center_document(_feature())
        d2 = rc_module.build_rec_center_document(
            _feature(REC_NAME="Other", LATITUDE=39.5, LONGITUDE=-105.0)
        )
        assert d1.metadata["map_url"] != d2.metadata["map_url"]

    def test_full_metadata_round_trips_as_json(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        parsed = json.loads(doc.metadata["full_metadata"])
        assert parsed["rec_name"] == "Scheitler Recreation Center"
        assert parsed["loc_code"] == "RB2"
        assert parsed["bldg_sqft"] == 26801

    def test_full_metadata_normalizes_literal_null_in_line2(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature(ADDRESS_LINE2="<Null>"))
        parsed = json.loads(doc.metadata["full_metadata"])
        assert parsed["address_line2"] is None

    def test_has_layers_false(self, rc_module):
        doc = rc_module.build_rec_center_document(_feature())
        assert doc.metadata["has_layers"] is False


# --- batch builder -----------------------------------------------------------


class TestBuildDocuments:
    def test_skips_features_without_coords(self, rc_module):
        features = [
            _feature(),
            _feature(REC_NAME="No Coords", LATITUDE=None, LONGITUDE=None),
            _feature(REC_NAME="Other", LATITUDE=39.5, LONGITUDE=-105.0),
        ]
        docs = rc_module.build_documents(features)
        assert len(docs) == 2
        names = {d.metadata["rec_name"] for d in docs}
        assert names == {"Scheitler Recreation Center", "Other"}

    def test_empty_features_returns_empty(self, rc_module):
        assert rc_module.build_documents([]) == []
