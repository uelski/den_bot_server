"""Tests for scripts/ingest_denver_non_public_schools.

Exercises the pure functions (cleaner, article picker, address formatter,
each sentence builder, page_content composition, build_school_document,
build_documents) — no Qdrant or network. Mirrors the test pattern in
test_ingest_denver_libraries.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "ingest_denver_non_public_schools.py"
)


@pytest.fixture(scope="module")
def schools_module():
    spec = importlib.util.spec_from_file_location(
        "ingest_denver_non_public_schools", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_non_public_schools"] = module
    spec.loader.exec_module(module)
    return module


def _props(**overrides) -> dict:
    base = {
        "OBJECTID": 1,
        "SCHOOL_DISTRICT": "Denver County 1",
        "SCHOOL_NUMBER": "4588",
        "SCHOOL_NAME": "Rick's Ctr for Gifted Children",
        "SCHOOL_TYPE": "Independent",
        "SCHOOL_LEVEL": "Primary",
        "GRADE_LEVELS": "PK-08",
        "NUM_STUDENTS": 226,
        "ADDRESS_LINE1": "2040 S. York St",
        "ADDRESS_LINE2": None,
        "CITY": "Denver",
        "STATE": "CO",
        "ZIP": "80210",
        "WEBSITE": "http://www.du.edu/ricks/index.html",
        "NOTES": None,
        "LAST_VERIFIED": "Thu, 07 Sep 2023 00:00:00 GMT",
        "ADDRESS_ID": None,
        "GLOBALID": "abc-123",
    }
    base.update(overrides)
    return base


def _feature(**prop_overrides) -> dict:
    return {
        "type": "Feature",
        "properties": _props(**prop_overrides),
        "geometry": {"type": "Point", "coordinates": [-104.960262, 39.679054]},
    }


# --- _clean -----------------------------------------------------------------


class TestClean:
    def test_returns_none_for_none(self, schools_module):
        assert schools_module._clean(None) is None

    def test_returns_none_for_empty_string(self, schools_module):
        assert schools_module._clean("") is None
        assert schools_module._clean("   ") is None

    def test_strips_whitespace(self, schools_module):
        assert schools_module._clean("  hello  ") == "hello"

    def test_coerces_numbers(self, schools_module):
        assert schools_module._clean(42) == "42"


# --- article picker ---------------------------------------------------------


class TestArticleFor:
    def test_vowel_starts_get_an(self, schools_module):
        for word in ("independent", "Independent", "open", "alpine"):
            assert schools_module._article_for(word) == "an"

    def test_consonant_starts_get_a(self, schools_module):
        for word in ("parochial", "Parochial", "private", "religious"):
            assert schools_module._article_for(word) == "a"

    def test_empty_defaults_to_a(self, schools_module):
        assert schools_module._article_for("") == "a"


# --- format_address ---------------------------------------------------------


class TestFormatAddress:
    def test_full_address_with_zip(self, schools_module):
        s = schools_module.format_address(_props())
        assert s == "2040 S. York St, Denver, CO 80210"

    def test_with_line2(self, schools_module):
        s = schools_module.format_address(_props(ADDRESS_LINE2="Suite 100"))
        assert s == "2040 S. York St, Suite 100, Denver, CO 80210"

    def test_missing_line1_returns_none(self, schools_module):
        assert schools_module.format_address(_props(ADDRESS_LINE1=None)) is None

    def test_missing_zip_omits_zip(self, schools_module):
        s = schools_module.format_address(_props(ZIP=None))
        assert s == "2040 S. York St, Denver, CO"

    def test_missing_city_falls_back_to_denver(self, schools_module):
        s = schools_module.format_address(_props(CITY=None))
        assert "Denver, CO" in s


# --- lead sentence ----------------------------------------------------------


class TestBuildLeadSentence:
    def test_independent_uses_an_article(self, schools_module):
        s = schools_module.build_lead_sentence(_props(SCHOOL_TYPE="Independent"))
        assert s.startswith(
            "Rick's Ctr for Gifted Children is an independent school located at"
        )

    def test_parochial_uses_a_article(self, schools_module):
        s = schools_module.build_lead_sentence(
            _props(SCHOOL_NAME="Mount Carmel", SCHOOL_TYPE="Parochial")
        )
        assert s.startswith("Mount Carmel is a parochial school located at")

    def test_school_type_lowercased(self, schools_module):
        # No "Independent" with capital I should appear in the sentence.
        s = schools_module.build_lead_sentence(_props())
        assert "Independent" not in s
        assert "independent" in s

    def test_missing_school_type_falls_back_to_non_public(self, schools_module):
        s = schools_module.build_lead_sentence(_props(SCHOOL_TYPE=None))
        assert "is a non-public school" in s

    def test_missing_address_uses_in_denver_fallback(self, schools_module):
        s = schools_module.build_lead_sentence(_props(ADDRESS_LINE1=None))
        assert s.endswith("school in Denver.")

    def test_missing_school_name_falls_back_to_default(self, schools_module):
        s = schools_module.build_lead_sentence(_props(SCHOOL_NAME=None))
        assert s.startswith("School is an independent school")


# --- grades sentence --------------------------------------------------------


class TestBuildGradesSentence:
    def test_full_with_grades_and_students(self, schools_module):
        s = schools_module.build_grades_sentence(_props())
        assert s == "It serves grades PK-08 with 226 students enrolled."

    def test_thousands_separator_on_enrollment(self, schools_module):
        s = schools_module.build_grades_sentence(_props(NUM_STUDENTS=1234))
        assert "1,234" in s

    def test_only_grades(self, schools_module):
        s = schools_module.build_grades_sentence(_props(NUM_STUDENTS=None))
        assert s == "It serves grades PK-08."

    def test_only_grades_when_students_zero(self, schools_module):
        s = schools_module.build_grades_sentence(_props(NUM_STUDENTS=0))
        assert s == "It serves grades PK-08."

    def test_only_students_no_grades(self, schools_module):
        s = schools_module.build_grades_sentence(
            _props(GRADE_LEVELS=None, NUM_STUDENTS=500)
        )
        assert s == "Enrollment: 500 students."

    def test_neither_returns_none(self, schools_module):
        s = schools_module.build_grades_sentence(
            _props(GRADE_LEVELS=None, NUM_STUDENTS=None)
        )
        assert s is None

    def test_invalid_num_students_treated_as_missing(self, schools_module):
        s = schools_module.build_grades_sentence(_props(NUM_STUDENTS="not a number"))
        # Falls through to grades-only path.
        assert s == "It serves grades PK-08."


# --- district / website / notes sentences ----------------------------------


class TestBuildDistrictSentence:
    def test_emits_district(self, schools_module):
        assert (
            schools_module.build_district_sentence("Denver County 1")
            == "Part of the Denver County 1 school district."
        )

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_district_sentence(None) is None
        assert schools_module.build_district_sentence("") is None


class TestBuildWebsiteSentence:
    def test_emits_website(self, schools_module):
        assert (
            schools_module.build_website_sentence("https://example.com")
            == "Website: https://example.com."
        )

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_website_sentence(None) is None
        assert schools_module.build_website_sentence("   ") is None


class TestBuildNotesSentence:
    def test_emits_notes_with_prefix(self, schools_module):
        s = schools_module.build_notes_sentence("Founded 1885. Religious affiliation.")
        assert s == "Notes: Founded 1885. Religious affiliation."

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_notes_sentence(None) is None


# --- page_content composition -----------------------------------------------


class TestBuildPageContent:
    def test_full_record_includes_all_sections(self, schools_module):
        text = schools_module.build_page_content(_props())
        assert "Rick's Ctr for Gifted Children is an independent school located at 2040 S. York St" in text
        assert "It serves grades PK-08 with 226 students enrolled." in text
        assert "Part of the Denver County 1 school district." in text
        assert "Website: http://www.du.edu/ricks/index.html." in text
        # NOTES is null in defaults; should be omitted.
        assert "Notes:" not in text

    def test_includes_notes_when_present(self, schools_module):
        text = schools_module.build_page_content(_props(NOTES="A historical note."))
        assert "Notes: A historical note." in text

    def test_sparse_record_keeps_lead_only(self, schools_module):
        text = schools_module.build_page_content(_props(
            GRADE_LEVELS=None, NUM_STUDENTS=None,
            SCHOOL_DISTRICT=None, WEBSITE=None, NOTES=None,
        ))
        # Lead present, nothing else.
        assert text.startswith("Rick's Ctr for Gifted Children is an independent school")
        assert "grades" not in text
        assert "district" not in text
        assert "Website" not in text

    def test_school_level_not_redundantly_in_text(self, schools_module):
        # SCHOOL_LEVEL is tracked in metadata for filterability but shouldn't
        # be duplicated in the embedded text (GRADE_LEVELS already covers it).
        text = schools_module.build_page_content(_props())
        assert "Primary" not in text  # SCHOOL_LEVEL value


# --- document construction --------------------------------------------------


class TestBuildSchoolDocument:
    def test_returns_none_for_missing_geometry(self, schools_module):
        feature = {"type": "Feature", "properties": _props(), "geometry": None}
        assert schools_module.build_school_document(feature) is None

    def test_returns_none_for_empty_coordinates(self, schools_module):
        feature = {
            "type": "Feature", "properties": _props(),
            "geometry": {"type": "Point", "coordinates": []},
        }
        assert schools_module.build_school_document(feature) is None

    def test_returns_none_for_invalid_coordinates(self, schools_module):
        feature = {
            "type": "Feature", "properties": _props(),
            "geometry": {"type": "Point", "coordinates": ["bad", "data"]},
        }
        assert schools_module.build_school_document(feature) is None

    def test_doc_has_expected_metadata_keys(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc is not None
        for key in (
            "doc_type", "institution_type", "school_name",
            "school_district", "school_number", "school_type",
            "school_level", "grade_levels", "num_students",
            "address_line1", "city", "zip", "website",
            "globalid", "objectid",
            "location", "service_name", "display_name",
            "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata",
        ):
            assert key in doc.metadata, f"missing metadata key {key!r}"

    def test_doc_type_is_shared_denver_school(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["doc_type"] == "denver_school"

    def test_institution_type_is_non_public(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["institution_type"] == "non-public"

    def test_school_type_preserved_from_source(self, schools_module):
        # The source SCHOOL_TYPE column ("Parochial"/"Independent") is stored
        # verbatim — separate from our institution_type discriminator.
        doc = schools_module.build_school_document(_feature(SCHOOL_TYPE="Parochial"))
        assert doc.metadata["school_type"] == "Parochial"
        assert doc.metadata["institution_type"] == "non-public"

    def test_service_name(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["service_name"] == "Denver Non-Public Schools"

    def test_display_name_is_school_name(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["display_name"] == "Rick's Ctr for Gifted Children"

    def test_location_uses_geometry_coordinates(self, schools_module):
        # geojson [lon, lat] → metadata.location {lat, lon}
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["location"] == {"lat": 39.679054, "lon": -104.960262}

    def test_map_url_is_per_school_google_maps(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["map_url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.679054,-104.960262"
        )

    def test_base_url_and_hub_url_match_dataset_constant(self, schools_module):
        d1 = schools_module.build_school_document(_feature())
        d2 = schools_module.build_school_document({
            "type": "Feature",
            "properties": _props(SCHOOL_NAME="Other"),
            "geometry": {"type": "Point", "coordinates": [-105.0, 39.5]},
        })
        assert d1.metadata["base_url"] == d2.metadata["base_url"]
        assert d1.metadata["base_url"] == d1.metadata["hub_url"]
        assert "non-public-school-2025-2026" in d1.metadata["base_url"]

    def test_per_school_map_urls_differ(self, schools_module):
        d1 = schools_module.build_school_document(_feature())
        d2 = schools_module.build_school_document({
            "type": "Feature",
            "properties": _props(SCHOOL_NAME="Other"),
            "geometry": {"type": "Point", "coordinates": [-105.0, 39.5]},
        })
        assert d1.metadata["map_url"] != d2.metadata["map_url"]

    def test_full_metadata_round_trips_as_json(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        parsed = json.loads(doc.metadata["full_metadata"])
        assert parsed["school_name"] == "Rick's Ctr for Gifted Children"
        assert parsed["school_type"] == "Independent"
        assert parsed["num_students"] == 226

    def test_has_layers_false(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["has_layers"] is False


# --- doc_filter -------------------------------------------------------------


class TestDocFilter:
    def test_filter_matches_doc_type_and_institution_type(self, schools_module):
        # The filter must include the institution_type condition so that a
        # --purge run on this script doesn't delete public-school docs that
        # share the same doc_type.
        flt = schools_module._doc_filter()
        # Two MUST conditions: doc_type and institution_type.
        assert len(flt.must) == 2
        keys = {cond.key for cond in flt.must}
        assert keys == {"metadata.doc_type", "metadata.institution_type"}


# --- batch builder -----------------------------------------------------------


class TestBuildDocuments:
    def test_skips_features_without_geometry(self, schools_module):
        features = [
            _feature(),
            {"type": "Feature", "properties": _props(SCHOOL_NAME="No Geom"), "geometry": None},
            _feature(SCHOOL_NAME="Other"),
        ]
        docs = schools_module.build_documents(features)
        assert len(docs) == 2
        names = {d.metadata["school_name"] for d in docs}
        assert names == {"Rick's Ctr for Gifted Children", "Other"}

    def test_empty_features_returns_empty(self, schools_module):
        assert schools_module.build_documents([]) == []
