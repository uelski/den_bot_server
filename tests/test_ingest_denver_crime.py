"""Tests for scripts/ingest_denver_crime.

Exercises the pure functions (bucket mapping, YoY trend, top offenses,
page_content composition, document construction) plus the geojson load and
PIP tagging on a small in-memory polygon set. No Qdrant or network.
Mirrors the test pattern in test_ingest_denver_parks.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_denver_crime.py"


@pytest.fixture(scope="module")
def crime_module():
    spec = importlib.util.spec_from_file_location("ingest_denver_crime", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_denver_crime"] = module
    spec.loader.exec_module(module)
    return module


# --- bucket mapping ---------------------------------------------------------


class TestBucketForCategory:
    def test_violent_buckets(self, crime_module):
        for cat in (
            "aggravated-assault", "murder", "robbery",
            "sexual-assault", "other-crimes-against-persons",
        ):
            assert crime_module.bucket_for_category(cat) == "violent"

    def test_property_buckets(self, crime_module):
        for cat in (
            "arson", "auto-theft", "burglary", "larceny",
            "theft-from-motor-vehicle", "white-collar-crime",
        ):
            assert crime_module.bucket_for_category(cat) == "property"

    def test_drug_bucket(self, crime_module):
        assert crime_module.bucket_for_category("drug-alcohol") == "drug"

    def test_public_disorder_bucket(self, crime_module):
        assert crime_module.bucket_for_category("public-disorder") == "public-disorder"

    def test_other_bucket(self, crime_module):
        assert crime_module.bucket_for_category("all-other-crimes") == "other"

    def test_unknown_category_falls_back_to_other(self, crime_module, caplog):
        # Reset the seen-set so this test is hermetic.
        crime_module._unknown_categories_seen.clear()
        with caplog.at_level("WARNING"):
            assert crime_module.bucket_for_category("brand-new-cat") == "other"
        assert any("brand-new-cat" in rec.message for rec in caplog.records)

    def test_unknown_category_warned_only_once(self, crime_module, caplog):
        crime_module._unknown_categories_seen.clear()
        with caplog.at_level("WARNING"):
            crime_module.bucket_for_category("novel-cat")
            crime_module.bucket_for_category("novel-cat")
            crime_module.bucket_for_category("novel-cat")
        warns = [r for r in caplog.records if "novel-cat" in r.message]
        assert len(warns) == 1


# --- YoY trend --------------------------------------------------------------


class TestComputeYoyTrend:
    def test_clearly_up(self, crime_module):
        assert crime_module.compute_yoy_trend({2020: 100, 2024: 200}) == "up"

    def test_clearly_down(self, crime_module):
        assert crime_module.compute_yoy_trend({2020: 200, 2024: 80}) == "down"

    def test_within_threshold_is_flat(self, crime_module):
        # 5% increase — under the 10% threshold
        assert crime_module.compute_yoy_trend({2020: 100, 2024: 105}) == "flat"

    def test_just_over_threshold_is_up(self, crime_module):
        # 11% increase — just over the threshold
        assert crime_module.compute_yoy_trend({2020: 100, 2024: 111}) == "up"

    def test_uses_oldest_and_newest_only(self, crime_module):
        # Middle years are noisy; only endpoints matter
        assert crime_module.compute_yoy_trend(
            {2020: 100, 2021: 500, 2022: 50, 2023: 800, 2024: 200}
        ) == "up"

    def test_single_year_is_flat(self, crime_module):
        assert crime_module.compute_yoy_trend({2024: 50}) == "flat"

    def test_empty_is_flat(self, crime_module):
        assert crime_module.compute_yoy_trend({}) == "flat"

    def test_zero_start_with_positive_end_is_up(self, crime_module):
        assert crime_module.compute_yoy_trend({2020: 0, 2024: 5}) == "up"

    def test_zero_start_zero_end_is_flat(self, crime_module):
        assert crime_module.compute_yoy_trend({2020: 0, 2024: 0}) == "flat"


# --- top offense types ------------------------------------------------------


class TestTopOffenseTypes:
    def test_returns_top_n_in_descending_order(self, crime_module):
        ids = ["a"] * 10 + ["b"] * 5 + ["c"] * 3 + ["d"] * 1
        result = crime_module.top_offense_types(ids, n=3)
        assert result == [("a", 10), ("b", 5), ("c", 3)]

    def test_returns_fewer_when_fewer_unique_values(self, crime_module):
        ids = ["a", "a", "b"]
        result = crime_module.top_offense_types(ids, n=5)
        assert result == [("a", 2), ("b", 1)]

    def test_empty_input_returns_empty(self, crime_module):
        assert crime_module.top_offense_types([]) == []


# --- page_content -----------------------------------------------------------


class TestBuildPageContent:
    def _stats(self, **overrides):
        base = dict(
            neighborhood_name="Five Points",
            total=1000,
            start_year=2021,
            end_year=2025,
            bucket_counts={
                "violent": 200, "property": 500, "drug": 100,
                "public-disorder": 150, "other": 50,
            },
            top_offenses=[
                ("theft-from-motor-vehicle", 250),
                ("larceny-other", 180),
                ("aggravated-assault", 120),
                ("drug-poss-marijuana", 90),
                ("burglary-residence-no-force", 60),
            ],
            yoy_trend="up",
            year_counts={2021: 150, 2022: 175, 2023: 200, 2024: 225, 2025: 250},
        )
        base.update(overrides)
        return base

    def test_full_record_includes_all_sections(self, crime_module):
        text = crime_module.build_page_content(**self._stats())
        assert "Five Points reported 1,000 criminal incidents from 2021 through 2025." in text
        assert "By category:" in text
        assert "property (500" in text  # ordered by descending count
        assert "violent (200" in text
        assert "The most common specific offenses were" in text
        assert "theft-from-motor-vehicle (250)" in text
        assert "increased from 150 in 2021 to 250 in 2025" in text

    def test_buckets_sorted_by_descending_count(self, crime_module):
        text = crime_module.build_page_content(**self._stats())
        # property (500) should appear before violent (200), etc.
        property_idx = text.index("property")
        violent_idx = text.index("violent")
        assert property_idx < violent_idx

    def test_zero_count_buckets_omitted_from_sentence(self, crime_module):
        stats = self._stats(
            bucket_counts={
                "violent": 100, "property": 200, "drug": 0,
                "public-disorder": 0, "other": 0,
            },
        )
        text = crime_module.build_page_content(**stats)
        # "drug (0...)" should not appear
        assert "drug (" not in text

    def test_no_loaded_safety_language(self, crime_module):
        # Make sure we don't accidentally call Five Points "dangerous" etc.
        text = crime_module.build_page_content(**self._stats())
        for forbidden in ("dangerous", "high-crime", "high crime", "safe", "unsafe"):
            assert forbidden.lower() not in text.lower()

    def test_down_trend_uses_decreased_verb(self, crime_module):
        stats = self._stats(
            yoy_trend="down",
            year_counts={2021: 500, 2022: 400, 2023: 300, 2024: 250, 2025: 200},
        )
        text = crime_module.build_page_content(**stats)
        assert "decreased" in text

    def test_flat_trend_uses_roughly_flat_phrase(self, crime_module):
        stats = self._stats(
            yoy_trend="flat",
            year_counts={2021: 200, 2022: 195, 2023: 205, 2024: 198, 2025: 202},
        )
        text = crime_module.build_page_content(**stats)
        assert "roughly flat" in text

    def test_single_year_window_omits_trend_sentence(self, crime_module):
        stats = self._stats(year_counts={2025: 100})
        text = crime_module.build_page_content(**stats)
        assert "Year-over-year" not in text


# --- polygon load + PIP -----------------------------------------------------


# A minimal geojson with two adjacent square neighborhoods covering
# (-105, 39.7) to (-104.8, 39.9). Western half = "West Test", eastern = "East Test".
MINI_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"NBHD_NAME": "West Test"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-105.0, 39.7], [-104.9, 39.7],
                    [-104.9, 39.9], [-105.0, 39.9], [-105.0, 39.7],
                ]],
            },
        },
        {
            "type": "Feature",
            "properties": {"NBHD_NAME": "East Test"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-104.9, 39.7], [-104.8, 39.7],
                    [-104.8, 39.9], [-104.9, 39.9], [-104.9, 39.7],
                ]],
            },
        },
    ],
}


@pytest.fixture
def mini_geojson_path(tmp_path):
    p = tmp_path / "mini_neighborhoods.geojson"
    p.write_text(json.dumps(MINI_GEOJSON))
    return p


class TestLoadNeighborhoodPolygons:
    def test_loads_names_polygons_centroids_aligned(self, crime_module, mini_geojson_path):
        names, polygons, centroids = crime_module.load_neighborhood_polygons(mini_geojson_path)
        assert names == ["West Test", "East Test"]
        assert len(polygons) == 2
        # Centroid of west square is (-104.95, 39.8). Lat=39.8, Lon=-104.95.
        assert centroids[0] == pytest.approx({"lat": 39.8, "lon": -104.95})
        assert centroids[1] == pytest.approx({"lat": 39.8, "lon": -104.85})

    def test_skips_features_without_name_or_geom(self, crime_module, tmp_path):
        gj = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}},  # no name
                {"type": "Feature", "properties": {"NBHD_NAME": "Lonely"}, "geometry": None},  # no geom
                MINI_GEOJSON["features"][0],
            ],
        }
        p = tmp_path / "partial.geojson"
        p.write_text(json.dumps(gj))
        names, polygons, _ = crime_module.load_neighborhood_polygons(p)
        assert names == ["West Test"]


class TestTagNeighborhoods:
    def test_points_tagged_to_correct_polygon(self, crime_module, mini_geojson_path):
        names, polygons, _ = crime_module.load_neighborhood_polygons(mini_geojson_path)
        df = pd.DataFrame({
            "GEO_LAT": [39.8, 39.8, 39.85],
            "GEO_LON": [-104.95, -104.85, -104.82],
        })
        tags = crime_module.tag_neighborhoods(df, names, polygons)
        assert tags.tolist() == ["West Test", "East Test", "East Test"]

    def test_points_outside_all_polygons_get_none(self, crime_module, mini_geojson_path):
        names, polygons, _ = crime_module.load_neighborhood_polygons(mini_geojson_path)
        df = pd.DataFrame({
            "GEO_LAT": [40.5, 39.0],   # north of, south of
            "GEO_LON": [-104.9, -104.9],
        })
        tags = crime_module.tag_neighborhoods(df, names, polygons)
        assert tags.tolist() == [None, None]

    def test_index_preserved(self, crime_module, mini_geojson_path):
        # tag_neighborhoods returns a Series aligned to df.index — important
        # because aggregate_per_neighborhood does a groupby on the assigned
        # column and assumes index alignment.
        names, polygons, _ = crime_module.load_neighborhood_polygons(mini_geojson_path)
        df = pd.DataFrame({
            "GEO_LAT": [39.8, 39.8],
            "GEO_LON": [-104.95, -104.85],
        }, index=[42, 99])
        tags = crime_module.tag_neighborhoods(df, names, polygons)
        assert list(tags.index) == [42, 99]


# --- aggregation ------------------------------------------------------------


class TestAggregatePerNeighborhood:
    def _df(self):
        return pd.DataFrame([
            # Five Points: 3 violent, 2 property, top offense = aggravated-assault
            {"neighborhood_name": "Five Points", "OFFENSE_CATEGORY_ID": "aggravated-assault",
             "OFFENSE_TYPE_ID": "aggravated-assault-strangulation", "year": 2021},
            {"neighborhood_name": "Five Points", "OFFENSE_CATEGORY_ID": "aggravated-assault",
             "OFFENSE_TYPE_ID": "aggravated-assault-strangulation", "year": 2022},
            {"neighborhood_name": "Five Points", "OFFENSE_CATEGORY_ID": "robbery",
             "OFFENSE_TYPE_ID": "robbery-business", "year": 2023},
            {"neighborhood_name": "Five Points", "OFFENSE_CATEGORY_ID": "larceny",
             "OFFENSE_TYPE_ID": "theft-shoplift", "year": 2024},
            {"neighborhood_name": "Five Points", "OFFENSE_CATEGORY_ID": "burglary",
             "OFFENSE_TYPE_ID": "burglary-residence-no-force", "year": 2025},
            # Capitol Hill: 1 incident
            {"neighborhood_name": "Capitol Hill", "OFFENSE_CATEGORY_ID": "drug-alcohol",
             "OFFENSE_TYPE_ID": "drug-poss-marijuana", "year": 2025},
        ])

    def test_groups_by_neighborhood(self, crime_module):
        result = crime_module.aggregate_per_neighborhood(self._df())
        assert set(result) == {"Five Points", "Capitol Hill"}

    def test_total_count_matches(self, crime_module):
        result = crime_module.aggregate_per_neighborhood(self._df())
        assert result["Five Points"]["total"] == 5
        assert result["Capitol Hill"]["total"] == 1

    def test_bucket_counts_correctly_mapped(self, crime_module):
        result = crime_module.aggregate_per_neighborhood(self._df())
        fp = result["Five Points"]["bucket_counts"]
        assert fp["violent"] == 3       # 2 aggravated-assault + 1 robbery
        assert fp["property"] == 2      # 1 larceny + 1 burglary
        assert fp["drug"] == 0
        assert fp["public-disorder"] == 0
        assert fp["other"] == 0

    def test_year_counts_have_python_int_keys(self, crime_module):
        # Numpy int64 keys break json.dumps downstream; make sure we coerce.
        result = crime_module.aggregate_per_neighborhood(self._df())
        for year in result["Five Points"]["year_counts"]:
            assert isinstance(year, int)
            assert not isinstance(year, bool)


# --- document construction --------------------------------------------------


class TestBuildDocuments:
    def _stats(self):
        return {
            "Five Points": {
                "total": 5,
                "year_counts": {2021: 1, 2022: 1, 2023: 1, 2024: 1, 2025: 1},
                "year_range": (2021, 2025),
                "bucket_counts": {
                    "violent": 3, "property": 2, "drug": 0,
                    "public-disorder": 0, "other": 0,
                },
                "top_offenses": [("aggravated-assault-strangulation", 2)],
                "yoy_trend": "flat",
            },
            "Empty": {
                "total": 0, "year_counts": {}, "year_range": (2025, 2025),
                "bucket_counts": {b: 0 for b in ("violent", "property", "drug", "public-disorder", "other")},
                "top_offenses": [], "yoy_trend": "flat",
            },
        }

    def _centroids(self):
        return {"Five Points": {"lat": 39.76, "lon": -104.97}, "Empty": {"lat": 0, "lon": 0}}

    def test_skips_neighborhoods_with_zero_incidents(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        names = [d.metadata["neighborhood_name"] for d in docs]
        assert names == ["Five Points"]

    def test_metadata_shape(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        d = docs[0]
        for key in (
            "doc_type", "neighborhood_name", "incident_count", "year_range",
            "yoy_trend", "service_name", "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata", "location",
        ):
            assert key in d.metadata, f"missing metadata key {key!r}"

    def test_doc_type_constant(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        assert docs[0].metadata["doc_type"] == "neighborhood_crime_summary"
        assert docs[0].metadata["service_name"] == "Denver Crime Statistics"

    def test_base_url_equals_hub_url(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        d = docs[0]
        assert d.metadata["base_url"] == d.metadata["hub_url"]
        assert "1e080d3ce2ae4e2698745a0d02345d4a_324/about" in d.metadata["base_url"]

    def test_map_url_is_explore_endpoint(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        assert "/explore" in docs[0].metadata["map_url"]

    def test_no_display_name_so_label_falls_back(self, crime_module):
        # Per the design call: omit display_name on crime docs because all
        # share the same map_url and per-neighborhood labels would mislead.
        docs = crime_module.build_documents(self._stats(), self._centroids())
        assert "display_name" not in docs[0].metadata

    def test_full_metadata_round_trips_as_json(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        parsed = json.loads(docs[0].metadata["full_metadata"])
        assert parsed["neighborhood_name"] == "Five Points"
        assert parsed["incident_count"] == 5
        assert parsed["bucket_counts"]["violent"] == 3

    def test_location_copied_from_centroid(self, crime_module):
        docs = crime_module.build_documents(self._stats(), self._centroids())
        assert docs[0].metadata["location"] == {"lat": 39.76, "lon": -104.97}

    def test_location_omitted_when_no_centroid(self, crime_module):
        docs = crime_module.build_documents(self._stats(), {})
        assert "location" not in docs[0].metadata
