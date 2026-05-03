"""Tests for scripts/ingest_denver_public_schools.

Exercises the pure functions (cleaner, article picker, get_display_name,
address formatter, each sentence builder, page_content composition,
build_school_document, build_documents) — no Qdrant or network. Mirrors
the test pattern in test_ingest_denver_non_public_schools.py with
adjustments for the public-schools schema (extra SCH_NAME_FULL field,
no NUM_STUDENTS, different SCHOOL_TYPE values).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "ingest_denver_public_schools.py"
)


@pytest.fixture(scope="module")
def schools_module():
    spec = importlib.util.spec_from_file_location(
        "ingest_denver_public_schools", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_public_schools"] = module
    spec.loader.exec_module(module)
    return module


def _props(**overrides) -> dict:
    base = {
        "OBJECTID": 1,
        "SCHOOL_DISTRICT": "Denver County 1 (DPS)",
        "SCHOOL_NUMBER": "453",
        "SCHOOL_NAME": "John F. Kennedy",
        "SCH_NAME_FULL": "John F. Kennedy High School",
        "SCHOOL_TYPE": "District",
        "SCHOOL_LEVEL": "High",
        "GRADE_LEVELS": "9-12",
        "NUM_STUDENTS": None,  # never populated for public schools
        "ADDRESS_LINE1": "2855 S. Lamar St.",
        "ADDRESS_LINE2": None,
        "CITY": "Denver",
        "STATE": "CO",
        "ZIP": "80227",
        "WEBSITE": "http://jfk.dpsk12.org",
        "NOTES": None,
        "LAST_VERIFIED": "Wed, 06 Sep 2023 00:00:00 GMT",
        "ADDRESS_ID": None,
        "GLOBALID": "abc-123",
    }
    base.update(overrides)
    return base


def _feature(**prop_overrides) -> dict:
    return {
        "type": "Feature",
        "properties": _props(**prop_overrides),
        "geometry": {"type": "Point", "coordinates": [-105.0734, 39.6512]},
    }


# --- _clean -----------------------------------------------------------------


class TestClean:
    def test_returns_none_for_none(self, schools_module):
        assert schools_module._clean(None) is None

    def test_returns_none_for_whitespace_only(self, schools_module):
        # NOTES in the source data is sometimes "  " — must normalize to None.
        assert schools_module._clean("  ") is None
        assert schools_module._clean("") is None

    def test_strips_whitespace(self, schools_module):
        assert schools_module._clean("  hello  ") == "hello"


# --- article picker ---------------------------------------------------------


class TestArticleFor:
    def test_district_charter_traditional_get_a(self, schools_module):
        # All public-school SCHOOL_TYPE values start with consonants.
        for word in ("district", "charter", "traditional"):
            assert schools_module._article_for(word) == "a"

    def test_vowel_starts_get_an(self, schools_module):
        # Defensive — covers future categories like "Alternative" or "Innovation".
        for word in ("alternative", "innovation", "open"):
            assert schools_module._article_for(word) == "an"


# --- get_display_name -------------------------------------------------------


class TestGetDisplayName:
    def test_prefers_sch_name_full(self, schools_module):
        name = schools_module.get_display_name(_props())
        assert name == "John F. Kennedy High School"

    def test_falls_back_to_school_name_when_full_missing(self, schools_module):
        name = schools_module.get_display_name(_props(SCH_NAME_FULL=None))
        assert name == "John F. Kennedy"

    def test_falls_back_to_school_name_when_full_empty_string(self, schools_module):
        name = schools_module.get_display_name(_props(SCH_NAME_FULL=""))
        assert name == "John F. Kennedy"

    def test_falls_back_to_default_when_both_missing(self, schools_module):
        name = schools_module.get_display_name(
            _props(SCH_NAME_FULL=None, SCHOOL_NAME=None)
        )
        assert name == "School"

    def test_strips_whitespace_in_chosen_name(self, schools_module):
        name = schools_module.get_display_name(
            _props(SCH_NAME_FULL="  Padded Name  ")
        )
        assert name == "Padded Name"


# --- format_address ---------------------------------------------------------


class TestFormatAddress:
    def test_full_address_with_zip(self, schools_module):
        s = schools_module.format_address(_props())
        assert s == "2855 S. Lamar St., Denver, CO 80227"

    def test_with_line2(self, schools_module):
        s = schools_module.format_address(_props(ADDRESS_LINE2="Bldg 2"))
        assert s == "2855 S. Lamar St., Bldg 2, Denver, CO 80227"

    def test_missing_line1_returns_none(self, schools_module):
        assert schools_module.format_address(_props(ADDRESS_LINE1=None)) is None

    def test_missing_zip_omits_zip(self, schools_module):
        s = schools_module.format_address(_props(ZIP=None))
        assert s == "2855 S. Lamar St., Denver, CO"

    def test_missing_city_falls_back_to_denver(self, schools_module):
        s = schools_module.format_address(_props(CITY=None))
        assert "Denver, CO" in s


# --- lead sentence ----------------------------------------------------------


class TestBuildLeadSentence:
    def test_district_school_uses_full_name(self, schools_module):
        s = schools_module.build_lead_sentence(_props())
        assert s == (
            "John F. Kennedy High School is a district school "
            "located at 2855 S. Lamar St., Denver, CO 80227."
        )

    def test_charter_school(self, schools_module):
        s = schools_module.build_lead_sentence(
            _props(SCHOOL_TYPE="Charter", SCH_NAME_FULL="STRIVE Prep - SMART Academy")
        )
        assert s.startswith(
            "STRIVE Prep - SMART Academy is a charter school located at"
        )

    def test_traditional_alternative_combo(self, schools_module):
        s = schools_module.build_lead_sentence(
            _props(SCHOOL_TYPE="Traditional / Alternative")
        )
        # Awkward but accurately reflects the source data.
        assert "is a traditional / alternative school" in s

    def test_school_type_lowercased(self, schools_module):
        s = schools_module.build_lead_sentence(_props())
        assert "District" not in s  # not the SCHOOL_TYPE value
        assert "district" in s

    def test_missing_school_type_falls_back_to_public(self, schools_module):
        s = schools_module.build_lead_sentence(_props(SCHOOL_TYPE=None))
        assert "is a public school" in s

    def test_missing_address_uses_in_denver_fallback(self, schools_module):
        s = schools_module.build_lead_sentence(_props(ADDRESS_LINE1=None))
        assert s.endswith("school in Denver.")

    def test_uses_school_name_when_full_missing(self, schools_module):
        s = schools_module.build_lead_sentence(_props(SCH_NAME_FULL=None))
        assert s.startswith("John F. Kennedy is a district school")


# --- grades sentence --------------------------------------------------------


class TestBuildGradesSentence:
    def test_grades_only_when_no_enrollment(self, schools_module):
        # Public schools have NUM_STUDENTS=None always; verify clean fallback.
        s = schools_module.build_grades_sentence(_props())
        assert s == "It serves grades 9-12."

    def test_with_enrollment_when_provided(self, schools_module):
        # Defensive: if NUM_STUDENTS gets backfilled later, the existing
        # logic should handle it without code changes.
        s = schools_module.build_grades_sentence(_props(NUM_STUDENTS=850))
        assert s == "It serves grades 9-12 with 850 students enrolled."

    def test_only_enrollment_no_grades(self, schools_module):
        s = schools_module.build_grades_sentence(
            _props(GRADE_LEVELS=None, NUM_STUDENTS=300)
        )
        assert s == "Enrollment: 300 students."

    def test_neither_returns_none(self, schools_module):
        assert schools_module.build_grades_sentence(
            _props(GRADE_LEVELS=None, NUM_STUDENTS=None)
        ) is None


# --- district / website / notes sentences ----------------------------------


class TestBuildDistrictSentence:
    def test_emits_district(self, schools_module):
        assert (
            schools_module.build_district_sentence("Denver County 1 (DPS)")
            == "Part of the Denver County 1 (DPS) school district."
        )

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_district_sentence(None) is None


class TestBuildWebsiteSentence:
    def test_emits_website(self, schools_module):
        assert (
            schools_module.build_website_sentence("http://jfk.dpsk12.org")
            == "Website: http://jfk.dpsk12.org."
        )

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_website_sentence(None) is None


class TestBuildNotesSentence:
    def test_emits_notes_with_prefix(self, schools_module):
        s = schools_module.build_notes_sentence("Magnet program in arts.")
        assert s == "Notes: Magnet program in arts."

    def test_whitespace_only_returns_none(self, schools_module):
        # NOTES in the source is sometimes "  "; must be omitted entirely.
        assert schools_module.build_notes_sentence("  ") is None

    def test_returns_none_when_missing(self, schools_module):
        assert schools_module.build_notes_sentence(None) is None


# --- page_content composition -----------------------------------------------


class TestBuildPageContent:
    def test_full_record_includes_all_sections(self, schools_module):
        text = schools_module.build_page_content(_props())
        assert "John F. Kennedy High School is a district school located at" in text
        # No enrollment phrase since public schools never have NUM_STUDENTS.
        assert "It serves grades 9-12." in text
        assert "students enrolled" not in text
        assert "Part of the Denver County 1 (DPS) school district." in text
        assert "Website: http://jfk.dpsk12.org." in text
        assert "Notes:" not in text  # NOTES is None in defaults

    def test_includes_notes_when_present(self, schools_module):
        text = schools_module.build_page_content(_props(NOTES="Magnet arts program."))
        assert "Notes: Magnet arts program." in text

    def test_sparse_record_keeps_lead_only(self, schools_module):
        text = schools_module.build_page_content(_props(
            GRADE_LEVELS=None, SCHOOL_DISTRICT=None, WEBSITE=None, NOTES=None,
        ))
        assert text.startswith("John F. Kennedy High School is a district school")
        assert "grades" not in text
        # Only the SCHOOL_DISTRICT sentence should be missing — the lead
        # still says "district school" via SCHOOL_TYPE.
        assert "school district" not in text
        assert "Website" not in text


# --- document construction --------------------------------------------------


class TestBuildSchoolDocument:
    def test_returns_none_for_missing_geometry(self, schools_module):
        feature = {"type": "Feature", "properties": _props(), "geometry": None}
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
            "doc_type", "institution_type",
            "school_name", "sch_name_full",
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

    def test_institution_type_is_public(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["institution_type"] == "public"

    def test_school_type_preserved_from_source(self, schools_module):
        # The source SCHOOL_TYPE column value stored verbatim.
        doc = schools_module.build_school_document(_feature(SCHOOL_TYPE="Charter"))
        assert doc.metadata["school_type"] == "Charter"
        assert doc.metadata["institution_type"] == "public"

    def test_service_name(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["service_name"] == "Denver Public Schools"

    def test_display_name_uses_full_name(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["display_name"] == "John F. Kennedy High School"

    def test_display_name_falls_back_to_short_name(self, schools_module):
        doc = schools_module.build_school_document(_feature(SCH_NAME_FULL=None))
        assert doc.metadata["display_name"] == "John F. Kennedy"

    def test_both_name_fields_preserved_in_metadata(self, schools_module):
        # Even though display_name uses SCH_NAME_FULL, the original SCHOOL_NAME
        # is still stored for filtering / cross-reference.
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["school_name"] == "John F. Kennedy"
        assert doc.metadata["sch_name_full"] == "John F. Kennedy High School"

    def test_location_uses_geometry_coordinates(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["location"] == {"lat": 39.6512, "lon": -105.0734}

    def test_map_url_is_per_school_google_maps(self, schools_module):
        doc = schools_module.build_school_document(_feature())
        assert doc.metadata["map_url"] == (
            "https://www.google.com/maps/search/?api=1&query=39.6512,-105.0734"
        )

    def test_base_url_and_hub_url_match_dataset_constant(self, schools_module):
        d1 = schools_module.build_school_document(_feature())
        d2 = schools_module.build_school_document({
            "type": "Feature",
            "properties": _props(SCHOOL_NAME="Other", SCH_NAME_FULL="Other High School"),
            "geometry": {"type": "Point", "coordinates": [-105.0, 39.5]},
        })
        assert d1.metadata["base_url"] == d2.metadata["base_url"]
        assert d1.metadata["base_url"] == d1.metadata["hub_url"]
        assert "public-school-2025-2026" in d1.metadata["base_url"]

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
        assert parsed["school_name"] == "John F. Kennedy"
        assert parsed["sch_name_full"] == "John F. Kennedy High School"
        assert parsed["school_type"] == "District"


# --- doc_filter -------------------------------------------------------------


class TestDocFilter:
    def test_filter_matches_doc_type_and_institution_type(self, schools_module):
        # The filter must include institution_type so a --purge run on this
        # script doesn't delete non-public-school docs sharing the same
        # doc_type.
        flt = schools_module._doc_filter()
        assert len(flt.must) == 2
        keys = {cond.key for cond in flt.must}
        assert keys == {"metadata.doc_type", "metadata.institution_type"}


# --- batch builder -----------------------------------------------------------


class TestBuildDocuments:
    def test_skips_features_without_geometry(self, schools_module):
        features = [
            _feature(),
            {"type": "Feature", "properties": _props(SCHOOL_NAME="No Geom"), "geometry": None},
            _feature(SCH_NAME_FULL="Other High School"),
        ]
        docs = schools_module.build_documents(features)
        assert len(docs) == 2
        names = {d.metadata["display_name"] for d in docs}
        assert names == {"John F. Kennedy High School", "Other High School"}

    def test_empty_features_returns_empty(self, schools_module):
        assert schools_module.build_documents([]) == []
