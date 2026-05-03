"""Tests for scripts/ingest_denver_traffic.

Exercises the pure functions (bucket mapping, YoY trend, sentence builders,
page_content composition, neighborhood filter, aggregation, document
construction) — no Qdrant or network. Mirrors the test pattern in
test_ingest_denver_crime.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_denver_traffic.py"


@pytest.fixture(scope="module")
def traffic_module():
    spec = importlib.util.spec_from_file_location("ingest_denver_traffic", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_traffic"] = module
    spec.loader.exec_module(module)
    return module


# --- bucket mapping ---------------------------------------------------------


class TestBucketForOffense:
    def test_all_six_known_buckets(self, traffic_module):
        cases = [
            ("TRAF - ACCIDENT", "general"),
            ("TRAF - ACCIDENT - HIT & RUN", "hit-and-run"),
            ("TRAF - ACCIDENT - DUI/DUID", "DUI/DUID"),
            ("TRAF - ACCIDENT - SBI", "serious bodily injury"),
            ("TRAF - ACCIDENT - POLICE", "police-involved"),
            ("TRAF - ACCIDENT - FATAL", "fatal"),
        ]
        for raw, expected in cases:
            assert traffic_module.bucket_for_offense(raw) == expected

    def test_strips_trailing_whitespace(self, traffic_module):
        # The source data has values like "TRAF - ACCIDENT               ".
        assert (
            traffic_module.bucket_for_offense("TRAF - ACCIDENT               ")
            == "general"
        )

    def test_none_returns_none(self, traffic_module):
        assert traffic_module.bucket_for_offense(None) is None

    def test_empty_returns_none(self, traffic_module):
        assert traffic_module.bucket_for_offense("") is None
        assert traffic_module.bucket_for_offense("   ") is None

    def test_unknown_returns_none_and_warns_once(self, traffic_module, caplog):
        traffic_module._unknown_offenses_seen.clear()
        with caplog.at_level("WARNING"):
            traffic_module.bucket_for_offense("TRAF - ACCIDENT - NEW THING")
            traffic_module.bucket_for_offense("TRAF - ACCIDENT - NEW THING")
            traffic_module.bucket_for_offense("TRAF - ACCIDENT - NEW THING")
        warns = [r for r in caplog.records if "NEW THING" in r.message]
        assert len(warns) == 1


# --- YoY trend --------------------------------------------------------------


class TestComputeYoyTrend:
    def test_clearly_up(self, traffic_module):
        assert traffic_module.compute_yoy_trend({2022: 100, 2026: 200}) == "up"

    def test_clearly_down(self, traffic_module):
        assert traffic_module.compute_yoy_trend({2022: 200, 2026: 80}) == "down"

    def test_within_threshold_is_flat(self, traffic_module):
        assert traffic_module.compute_yoy_trend({2022: 100, 2026: 105}) == "flat"

    def test_uses_oldest_and_newest_only(self, traffic_module):
        assert traffic_module.compute_yoy_trend(
            {2022: 100, 2023: 500, 2024: 50, 2025: 800, 2026: 200}
        ) == "up"

    def test_single_year_is_flat(self, traffic_module):
        assert traffic_module.compute_yoy_trend({2026: 50}) == "flat"

    def test_zero_start_with_positive_end_is_up(self, traffic_module):
        assert traffic_module.compute_yoy_trend({2022: 0, 2026: 5}) == "up"


# --- type sentence ----------------------------------------------------------


class TestBuildTypeSentence:
    def test_descending_order_with_zeros_omitted(self, traffic_module):
        s = traffic_module.build_type_sentence({
            "general": 1000,
            "hit-and-run": 400,
            "DUI/DUID": 50,
            "serious bodily injury": 0,
            "police-involved": 25,
            "fatal": 5,
        })
        # Order: general (1000), hit-and-run (400), DUI/DUID (50),
        # police-involved (25), fatal (5). Zero-count buckets omitted.
        assert s == (
            "By type: general (1,000), hit-and-run (400), DUI/DUID (50), "
            "police-involved (25), fatal (5)."
        )
        assert "serious bodily injury" not in s

    def test_all_zero_returns_none(self, traffic_module):
        s = traffic_module.build_type_sentence({label: 0 for label in (
            "general", "hit-and-run", "DUI/DUID",
            "serious bodily injury", "police-involved", "fatal",
        )})
        assert s is None

    def test_thousands_separator(self, traffic_module):
        s = traffic_module.build_type_sentence({"general": 12345})
        assert "12,345" in s


# --- severity sentence ------------------------------------------------------


class TestBuildSeveritySentence:
    def test_both_killed_and_injured(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=12, seriously_injured=78)
        assert s == (
            "Across these incidents, 12 people were killed "
            "and 78 were seriously injured."
        )

    def test_only_killed(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=5, seriously_injured=0)
        assert s == "Across these incidents, 5 people were killed."

    def test_only_injured(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=0, seriously_injured=20)
        assert s == "Across these incidents, 20 people were seriously injured."

    def test_singular_killed(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=1, seriously_injured=0)
        assert s == "Across these incidents, 1 person was killed."

    def test_singular_injured(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=0, seriously_injured=1)
        assert s == "Across these incidents, 1 person was seriously injured."

    def test_singular_killed_with_plural_injured(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=1, seriously_injured=10)
        assert "1 person was killed" in s
        assert "10 were seriously injured" in s

    def test_zero_returns_none(self, traffic_module):
        assert traffic_module.build_severity_sentence(0, 0) is None

    def test_thousands_separator(self, traffic_module):
        s = traffic_module.build_severity_sentence(fatalities=1234, seriously_injured=5678)
        assert "1,234" in s
        assert "5,678" in s


# --- vulnerable road users sentence -----------------------------------------


class TestBuildVruSentence:
    def test_both_pedestrian_and_bicycle(self, traffic_module):
        s = traffic_module.build_vru_sentence(pedestrian_count=142, bicycle_count=67)
        assert s == "142 accidents involved pedestrians and 67 involved bicyclists."

    def test_only_pedestrian(self, traffic_module):
        s = traffic_module.build_vru_sentence(pedestrian_count=42, bicycle_count=0)
        assert s == "42 accidents involved pedestrians."

    def test_only_bicycle(self, traffic_module):
        s = traffic_module.build_vru_sentence(pedestrian_count=0, bicycle_count=15)
        assert s == "15 accidents involved bicyclists."

    def test_zero_returns_none(self, traffic_module):
        assert traffic_module.build_vru_sentence(0, 0) is None


# --- trend sentence ---------------------------------------------------------


class TestBuildTrendSentence:
    def test_up_trend(self, traffic_module):
        s = traffic_module.build_trend_sentence(
            {2022: 700, 2023: 750, 2024: 800, 2025: 900, 2026: 950},
            "up",
        )
        assert s == (
            "Year-over-year, accident counts increased from "
            "700 in 2022 to 950 in 2026."
        )

    def test_down_trend(self, traffic_module):
        s = traffic_module.build_trend_sentence(
            {2022: 1000, 2026: 600}, "down",
        )
        assert "decreased" in s

    def test_flat_trend(self, traffic_module):
        s = traffic_module.build_trend_sentence(
            {2022: 500, 2026: 510}, "flat",
        )
        assert "stayed roughly flat" in s

    def test_single_year_returns_none(self, traffic_module):
        assert traffic_module.build_trend_sentence({2026: 100}, "flat") is None


# --- page_content composition -----------------------------------------------


class TestBuildPageContent:
    def _stats(self, **overrides):
        base = dict(
            neighborhood_name="Five Points",
            total=4237,
            start_year=2022,
            end_year=2026,
            bucket_counts={
                "general": 2500,
                "hit-and-run": 1200,
                "DUI/DUID": 200,
                "serious bodily injury": 80,
                "police-involved": 50,
                "fatal": 18,
            },
            fatalities=22,
            seriously_injured=78,
            pedestrian_count=142,
            bicycle_count=67,
            year_counts={2022: 700, 2023: 750, 2024: 800, 2025: 900, 2026: 950},
            yoy_trend="up",
        )
        base.update(overrides)
        return base

    def test_full_record_includes_all_sections(self, traffic_module):
        text = traffic_module.build_page_content(**self._stats())
        assert "Five Points reported 4,237 traffic accidents from 2022 through 2026." in text
        assert "By type:" in text
        assert "general (2,500)" in text  # ordered by descending count
        assert "Across these incidents, 22 people were killed and 78 were seriously injured." in text
        assert "142 accidents involved pedestrians and 67 involved bicyclists." in text
        assert "increased from 700 in 2022 to 950 in 2026" in text

    def test_no_loaded_safety_language(self, traffic_module):
        text = traffic_module.build_page_content(**self._stats())
        for forbidden in ("dangerous", "high-crime", "high crime", "safe", "unsafe"):
            assert forbidden.lower() not in text.lower()

    def test_omits_severity_when_no_casualties(self, traffic_module):
        text = traffic_module.build_page_content(
            **self._stats(fatalities=0, seriously_injured=0)
        )
        assert "killed" not in text
        assert "seriously injured" not in text

    def test_omits_vru_when_zero(self, traffic_module):
        text = traffic_module.build_page_content(
            **self._stats(pedestrian_count=0, bicycle_count=0)
        )
        assert "pedestrians" not in text
        assert "bicyclists" not in text

    def test_minimal_record_keeps_only_lead(self, traffic_module):
        text = traffic_module.build_page_content(**self._stats(
            bucket_counts={label: 0 for label in (
                "general", "hit-and-run", "DUI/DUID",
                "serious bodily injury", "police-involved", "fatal",
            )},
            fatalities=0, seriously_injured=0,
            pedestrian_count=0, bicycle_count=0,
            year_counts={2026: 100},  # single year — trend omitted
        ))
        assert text.startswith("Five Points reported 4,237 traffic accidents")
        assert "By type:" not in text
        assert "Year-over-year" not in text


# --- neighborhood filter ----------------------------------------------------


class TestFilterToKnownNeighborhoods:
    def test_drops_unknown_and_missing_names(self, traffic_module):
        df = pd.DataFrame({
            "neighborhood_id": ["Five Points", "", "Capitol Hill", "Bogus Place", "CBD"],
        })
        known = {"Five Points", "Capitol Hill", "CBD"}
        result = traffic_module.filter_to_known_neighborhoods(df, known)
        assert sorted(result["neighborhood_id"].tolist()) == ["CBD", "Capitol Hill", "Five Points"]


# --- aggregation ------------------------------------------------------------


class TestAggregatePerNeighborhood:
    def _df(self):
        return pd.DataFrame([
            # Five Points: 5 incidents, 1 hit-and-run, 1 fatal, 1 DUI, 2 general
            {"neighborhood_id": "Five Points",
             "top_traffic_accident_offense": "TRAF - ACCIDENT - HIT & RUN",
             "year": 2022, "FATALITIES": 0, "SERIOUSLY_INJURED": 0,
             "pedestrian_ind": 1, "bicycle_ind": 0},
            {"neighborhood_id": "Five Points",
             "top_traffic_accident_offense": "TRAF - ACCIDENT - FATAL",
             "year": 2023, "FATALITIES": 2, "SERIOUSLY_INJURED": 1,
             "pedestrian_ind": 0, "bicycle_ind": 0},
            {"neighborhood_id": "Five Points",
             "top_traffic_accident_offense": "TRAF - ACCIDENT - DUI/DUID",
             "year": 2024, "FATALITIES": 0, "SERIOUSLY_INJURED": 0,
             "pedestrian_ind": 0, "bicycle_ind": 1},
            {"neighborhood_id": "Five Points",
             "top_traffic_accident_offense": "TRAF - ACCIDENT",
             "year": 2025, "FATALITIES": 0, "SERIOUSLY_INJURED": 0,
             "pedestrian_ind": 0, "bicycle_ind": 0},
            {"neighborhood_id": "Five Points",
             "top_traffic_accident_offense": "TRAF - ACCIDENT",
             "year": 2026, "FATALITIES": 0, "SERIOUSLY_INJURED": 1,
             "pedestrian_ind": 1, "bicycle_ind": 0},
            # CBD: 2 incidents
            {"neighborhood_id": "CBD",
             "top_traffic_accident_offense": "TRAF - ACCIDENT - SBI",
             "year": 2026, "FATALITIES": 0, "SERIOUSLY_INJURED": 3,
             "pedestrian_ind": 0, "bicycle_ind": 0},
            {"neighborhood_id": "CBD",
             "top_traffic_accident_offense": "TRAF - ACCIDENT",
             "year": 2026, "FATALITIES": 0, "SERIOUSLY_INJURED": 0,
             "pedestrian_ind": 0, "bicycle_ind": 0},
        ])

    def test_groups_by_neighborhood(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        assert set(result) == {"Five Points", "CBD"}

    def test_total_counts(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        assert result["Five Points"]["total"] == 5
        assert result["CBD"]["total"] == 2

    def test_bucket_counts(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        fp = result["Five Points"]["bucket_counts"]
        assert fp["hit-and-run"] == 1
        assert fp["fatal"] == 1
        assert fp["DUI/DUID"] == 1
        assert fp["general"] == 2
        assert fp["serious bodily injury"] == 0

    def test_severity_sums(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        assert result["Five Points"]["fatalities"] == 2
        assert result["Five Points"]["seriously_injured"] == 2  # 1 + 1
        assert result["CBD"]["fatalities"] == 0
        assert result["CBD"]["seriously_injured"] == 3

    def test_vru_counts(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        assert result["Five Points"]["pedestrian_count"] == 2  # 2 incidents with ped_ind=1
        assert result["Five Points"]["bicycle_count"] == 1

    def test_year_counts_have_python_int_keys(self, traffic_module):
        result = traffic_module.aggregate_per_neighborhood(self._df())
        for year in result["Five Points"]["year_counts"]:
            assert isinstance(year, int)
            assert not isinstance(year, bool)


# --- document construction --------------------------------------------------


class TestBuildDocuments:
    def _stats(self):
        return {
            "Five Points": {
                "total": 5,
                "year_counts": {2022: 1, 2023: 1, 2024: 1, 2025: 1, 2026: 1},
                "year_range": (2022, 2026),
                "bucket_counts": {
                    "general": 2, "hit-and-run": 1, "DUI/DUID": 1,
                    "serious bodily injury": 0, "police-involved": 0, "fatal": 1,
                },
                "fatalities": 2,
                "seriously_injured": 2,
                "pedestrian_count": 2,
                "bicycle_count": 1,
                "yoy_trend": "flat",
            },
            "Empty": {
                "total": 0, "year_counts": {}, "year_range": (2026, 2026),
                "bucket_counts": {label: 0 for label in (
                    "general", "hit-and-run", "DUI/DUID",
                    "serious bodily injury", "police-involved", "fatal",
                )},
                "fatalities": 0, "seriously_injured": 0,
                "pedestrian_count": 0, "bicycle_count": 0,
                "yoy_trend": "flat",
            },
        }

    def _centroids(self):
        return {"Five Points": {"lat": 39.76, "lon": -104.97}}

    def test_skips_neighborhoods_with_zero_incidents(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        names = [d.metadata["neighborhood_name"] for d in docs]
        assert names == ["Five Points"]

    def test_metadata_shape(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        d = docs[0]
        for key in (
            "doc_type", "neighborhood_name", "incident_count", "year_range",
            "fatalities", "seriously_injured",
            "pedestrian_count", "bicycle_count", "yoy_trend",
            "service_name", "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata", "location",
        ):
            assert key in d.metadata, f"missing metadata key {key!r}"

    def test_doc_type_constant(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        assert docs[0].metadata["doc_type"] == "neighborhood_traffic_summary"
        assert docs[0].metadata["service_name"] == "Denver Traffic Accidents"

    def test_base_url_equals_hub_url(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        d = docs[0]
        assert d.metadata["base_url"] == d.metadata["hub_url"]
        assert "db00bd99ea534d8987e0913a191ebe19_325/about" in d.metadata["base_url"]

    def test_map_url_is_explore_endpoint(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        assert docs[0].metadata["map_url"].endswith("/explore")
        assert "geospatialDenver::traffic-accidents-offenses" in docs[0].metadata["map_url"]

    def test_no_display_name_so_label_falls_back(self, traffic_module):
        # Same design call as crime: omit display_name because all docs share
        # the same map_url; per-neighborhood labels would mislead.
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        assert "display_name" not in docs[0].metadata

    def test_full_metadata_round_trips_as_json(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        parsed = json.loads(docs[0].metadata["full_metadata"])
        assert parsed["neighborhood_name"] == "Five Points"
        assert parsed["incident_count"] == 5
        assert parsed["fatalities"] == 2
        assert parsed["bucket_counts"]["hit-and-run"] == 1

    def test_location_copied_from_centroid(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), self._centroids())
        assert docs[0].metadata["location"] == {"lat": 39.76, "lon": -104.97}

    def test_location_omitted_when_no_centroid(self, traffic_module):
        docs = traffic_module.build_documents(self._stats(), {})
        assert "location" not in docs[0].metadata
