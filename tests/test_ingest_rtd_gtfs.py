"""Unit tests for scripts.ingest_rtd_gtfs.

Pure-data test fixture: 3 stops, 2 routes, a small handful of stop_times rows.
The Qdrant upsert path is not exercised here — only the join + document builder
logic, which is the failure-prone part.
"""

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest_rtd_gtfs.py"


@pytest.fixture(scope="module")
def gtfs_module():
    """Import scripts/ingest_rtd_gtfs.py as a module without going through the
    project's package machinery (scripts/ isn't a package)."""
    spec = importlib.util.spec_from_file_location("ingest_rtd_gtfs", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stops_df():
    return pd.DataFrame(
        [
            # Three boarding stops + one parent station that should be filtered
            {
                "stop_id": "S1",
                "stop_code": "1001",
                "stop_name": "Colfax & Washington",
                "stop_lat": "39.7401",
                "stop_lon": "-104.9762",
                "location_type": "0",
                "wheelchair_boarding": "1",
            },
            {
                "stop_id": "S2",
                "stop_code": "1002",
                "stop_name": "Welton & 30th",
                "stop_lat": "39.7591",
                "stop_lon": "-104.9834",
                "location_type": "0",
                "wheelchair_boarding": "0",
            },
            {
                "stop_id": "S3",
                "stop_code": "1003",
                "stop_name": "Speer & Lincoln",
                "stop_lat": "39.7340",
                "stop_lon": "-104.9876",
                "location_type": "0",
                "wheelchair_boarding": "2",
            },
            {
                # parent station — should be skipped
                "stop_id": "STATION_X",
                "stop_code": "",
                "stop_name": "Union Station",
                "stop_lat": "39.7530",
                "stop_lon": "-105.0001",
                "location_type": "1",
                "wheelchair_boarding": "1",
            },
        ]
    )
    # types: stop_lat/lon are float in load_gtfs; we convert in the fixture below.


@pytest.fixture
def stops_df_typed(stops_df):
    df = stops_df.copy()
    df["stop_lat"] = df["stop_lat"].astype(float)
    df["stop_lon"] = df["stop_lon"].astype(float)
    return df


@pytest.fixture
def routes_df():
    df = pd.DataFrame(
        [
            {
                "route_id": "R1",
                "agency_id": "RTD",
                "route_short_name": "15",
                "route_long_name": "Colfax",
                "route_type": "3",
                "route_color": "0066CC",
            },
            {
                "route_id": "103W",
                "agency_id": "RTD",
                "route_short_name": "W",
                "route_long_name": "W Line",
                "route_type": "0",
                "route_color": "FF0000",
            },
        ]
    )
    df["route_type"] = df["route_type"].astype(int)
    return df


@pytest.fixture
def trips_df():
    return pd.DataFrame(
        [
            {"trip_id": "T1", "route_id": "R1"},
            {"trip_id": "T2", "route_id": "R1"},
            {"trip_id": "T3", "route_id": "103W"},
        ]
    )


@pytest.fixture
def stop_times_df():
    return pd.DataFrame(
        [
            # T1 (route R1) visits S1, S2
            {"trip_id": "T1", "stop_id": "S1"},
            {"trip_id": "T1", "stop_id": "S2"},
            # T2 (route R1) also visits S1 (dup pair, should dedupe)
            {"trip_id": "T2", "stop_id": "S1"},
            # T3 (route R2) visits S2, S3
            {"trip_id": "T3", "stop_id": "S2"},
            {"trip_id": "T3", "stop_id": "S3"},
        ]
    )


class TestStopRouteIndex:
    def test_join_and_dedupe(self, gtfs_module, stop_times_df, trips_df):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        assert index["S1"] == ["R1"]           # T1 + T2 both -> R1, dedupes
        assert sorted(index["S2"]) == ["103W", "R1"]
        assert index["S3"] == ["103W"]

    def test_route_lists_are_sorted(self, gtfs_module, stop_times_df, trips_df):
        """Deterministic output makes downstream embedding text reproducible."""
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        for route_ids in index.values():
            assert route_ids == sorted(route_ids)


class TestBuildStopDocuments:
    def test_skips_parent_stations(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        # 3 boarding stops, parent station STATION_X excluded.
        assert len(docs) == 3
        stop_ids = {d.metadata["stop_id"] for d in docs}
        assert stop_ids == {"S1", "S2", "S3"}

    def test_skips_stops_with_no_routes(
        self, gtfs_module, stops_df_typed, routes_df
    ):
        # Empty index -> no stop docs even though the stops exist
        docs = gtfs_module.build_stop_documents(stops_df_typed, {}, routes_df)
        assert docs == []

    def test_metadata_shape(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        s2 = next(d for d in docs if d.metadata["stop_id"] == "S2")

        # Core metadata keys
        for key in (
            "doc_type", "stop_id", "stop_code", "stop_name", "location",
            "route_ids", "route_short_names", "route_count",
            "service_name", "display_name",
            "base_url", "hub_url", "map_url",
            "has_layers", "full_metadata",
        ):
            assert key in s2.metadata, f"missing metadata key {key!r}"

        assert s2.metadata["doc_type"] == "rtd_stop"
        assert s2.metadata["service_name"] == "RTD Transit (GTFS)"
        assert s2.metadata["display_name"] == "Welton & 30th"
        assert s2.metadata["has_layers"] is False
        assert s2.metadata["location"] == {"lat": 39.7591, "lon": -104.9834}
        assert sorted(s2.metadata["route_short_names"]) == ["15", "W"]
        assert s2.metadata["route_count"] == 2

    def test_stop_urls_split_hub_vs_map(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        # base_url + hub_url collapse to the RTD citation root (constant across
        # all stops); map_url is the per-stop NextRide deep link.
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        s1 = next(d for d in docs if d.metadata["stop_id"] == "S1")
        s2 = next(d for d in docs if d.metadata["stop_id"] == "S2")
        assert s1.metadata["base_url"] == "https://app.rtd-denver.com/"
        assert s1.metadata["hub_url"] == "https://app.rtd-denver.com/"
        assert s1.metadata["base_url"] == s2.metadata["base_url"]
        assert s1.metadata["map_url"] == "https://app.rtd-denver.com/nextride/stop/S1"
        assert s2.metadata["map_url"] == "https://app.rtd-denver.com/nextride/stop/S2"

    def test_page_content_includes_routes_and_location(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        s2 = next(d for d in docs if d.metadata["stop_id"] == "S2")
        text = s2.page_content
        # Stop name appears
        assert "Welton & 30th" in text
        # Both routes serving S2 appear
        assert "15" in text and "W" in text
        # Coordinates appear (formatted to 5 decimals in template)
        assert "39.75910" in text
        assert "-104.98340" in text

    def test_wheelchair_text_branches(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        s1 = next(d for d in docs if d.metadata["stop_id"] == "S1")  # value 1
        s2 = next(d for d in docs if d.metadata["stop_id"] == "S2")  # value 0
        s3 = next(d for d in docs if d.metadata["stop_id"] == "S3")  # value 2
        assert "Wheelchair accessible." in s1.page_content
        assert "Wheelchair" not in s2.page_content
        assert "Not wheelchair accessible." in s3.page_content

    def test_full_metadata_round_trips_as_json(
        self, gtfs_module, stops_df_typed, stop_times_df, trips_df, routes_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_stop_documents(stops_df_typed, index, routes_df)
        s1 = next(d for d in docs if d.metadata["stop_id"] == "S1")
        parsed = json.loads(s1.metadata["full_metadata"])
        assert parsed["stop_id"] == "S1"
        assert parsed["route_ids"] == ["R1"]


class TestBuildRouteDocuments:
    def test_one_doc_per_route(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        assert len(docs) == 2

    def test_metadata_shape(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        r2 = next(d for d in docs if d.metadata["route_id"] == "103W")
        assert r2.metadata["doc_type"] == "rtd_route"
        assert r2.metadata["route_short_name"] == "W"
        assert r2.metadata["route_type"] == 0
        assert r2.metadata["route_type_label"] == "Light Rail"
        # R2 (W Line) serves S2 + S3 = 2 stops
        assert r2.metadata["stop_count"] == 2

    def test_route_type_label_mapping(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        labels = {d.metadata["route_id"]: d.metadata["route_type_label"] for d in docs}
        assert labels["R1"] == "Bus"        # type 3
        assert labels["103W"] == "Light Rail" # type 0

    def test_route_urls_split_hub_vs_map(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        # Slug rule (see _rtd_static.nextride_route_slug):
        #   - rail-shape ids ("103W") use route_short_name -> /route/W
        #   - everything else uses route_id -> /route/R1
        # base_url + hub_url collapse to the RTD citation root; map_url is
        # the per-route NextRide deep link.
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        r1 = next(d for d in docs if d.metadata["route_id"] == "R1")
        assert r1.metadata["base_url"] == "https://app.rtd-denver.com/"
        assert r1.metadata["hub_url"] == "https://app.rtd-denver.com/"
        # R1 doesn't match ^\d+[A-Z]+$, so route_id wins as the slug.
        assert r1.metadata["map_url"] == "https://app.rtd-denver.com/nextride/route/R1"
        r2 = next(d for d in docs if d.metadata["route_id"] == "103W")
        assert r2.metadata["base_url"] == "https://app.rtd-denver.com/"
        # 103W matches rail regex, so short_name wins.
        assert r2.metadata["map_url"] == "https://app.rtd-denver.com/nextride/route/W"

    def test_route_display_name_templated_when_both_names_present(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        r1 = next(d for d in docs if d.metadata["route_id"] == "R1")
        # short=15, long=Colfax -> "15 - Colfax"
        assert r1.metadata["display_name"] == "15 - Colfax"

    def test_route_display_name_collapses_when_short_equals_long(
        self, gtfs_module, stop_times_df, trips_df
    ):
        # When short_name and long_name are identical (e.g. "W"/"W"), avoid
        # the awkward "W - W" by using just the single name.
        df = pd.DataFrame([{
            "route_id": "1", "agency_id": "RTD",
            "route_short_name": "W", "route_long_name": "W",
            "route_type": "0", "route_color": "FF0000",
        }])
        df["route_type"] = df["route_type"].astype(int)
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(df, index, "RTD")
        assert docs[0].metadata["display_name"] == "W"

    def test_route_display_name_falls_back_to_short_only(
        self, gtfs_module, stop_times_df, trips_df
    ):
        df = pd.DataFrame([{
            "route_id": "FREE", "agency_id": "RTD",
            "route_short_name": "FREE", "route_long_name": "",
            "route_type": "3", "route_color": "",
        }])
        df["route_type"] = df["route_type"].astype(int)
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(df, index, "RTD")
        assert docs[0].metadata["display_name"] == "FREE"

    def test_page_content_uses_long_name_and_label(
        self, gtfs_module, routes_df, stop_times_df, trips_df
    ):
        index = gtfs_module.build_stop_route_index(stop_times_df, trips_df)
        docs = gtfs_module.build_route_documents(routes_df, index, "RTD")
        r2 = next(d for d in docs if d.metadata["route_id"] == "103W")
        text = r2.page_content
        assert "W Line" in text
        assert "Light Rail" in text
        assert "RTD" in text


class TestRouteTypeLabels:
    def test_known_values_present(self, gtfs_module):
        labels = gtfs_module.ROUTE_TYPE_LABELS
        assert labels[0] == "Light Rail"
        assert labels[3] == "Bus"
        assert labels[2] == "Commuter Rail"
