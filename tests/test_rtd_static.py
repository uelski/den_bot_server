"""Tests for app.tools._rtd_static.

Use a tiny in-memory GTFS fixture (writes a few CSV files into a temp dir,
points GTFS_DIR at it) so the tests don't depend on the full RTD feed being
present. Each test resets the lru_caches via _rtd_static.clear_caches().
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.tools import _rtd_static


@pytest.fixture
def tiny_gtfs(tmp_path: Path, monkeypatch) -> Path:
    """Write a 3-route, 4-stop, 3-trip GTFS subset and rewire GTFS_DIR.

    Two of the four stops share `stop_name` so the pairing index has work to
    do. One stop has location_type=1 (parent station) so it should be skipped.
    """
    gtfs_dir = tmp_path / "rtd_gtfs"
    gtfs_dir.mkdir()

    (gtfs_dir / "stops.txt").write_text(textwrap.dedent("""\
        stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,location_type,parent_station,wheelchair_boarding
        S1,26134,38th & Blake,Vehicles Travelling West,39.7700,-104.9800,0,,1
        S2,26135,38th & Blake,Vehicles Travelling East,39.7701,-104.9799,0,,1
        S3,26136,Union Station,Track 1,39.7530,-105.0000,0,,1
        SP,,Union Station Parent,,39.7530,-105.0000,1,,
    """))

    (gtfs_dir / "routes.txt").write_text(textwrap.dedent("""\
        route_id,route_short_name,route_long_name,route_type,route_color
        W,W,W Line,0,FFCC00
        15,15,Colfax,3,
        A,A,A Line,2,1B5E20
    """))

    (gtfs_dir / "trips.txt").write_text(textwrap.dedent("""\
        route_id,service_id,trip_id,trip_headsign,direction_id
        W,WK,t_w_in,Union Station,0
        W,WK,t_w_out,Federal Center,1
        15,WK,t_15_e,East,0
    """))

    monkeypatch.setattr(_rtd_static, "GTFS_DIR", gtfs_dir)
    _rtd_static.clear_caches()
    yield gtfs_dir
    _rtd_static.clear_caches()


class TestRoutesById:
    def test_loads_all_routes(self, tiny_gtfs):
        routes = _rtd_static.routes_by_id()
        assert set(routes) == {"W", "15", "A"}

    def test_route_record_fields(self, tiny_gtfs):
        w = _rtd_static.routes_by_id()["W"]
        assert w.short_name == "W"
        assert w.long_name == "W Line"
        assert w.route_type == 0
        assert w.type_label == "Light Rail"
        assert w.route_color == "FFCC00"

    def test_route_color_empty_becomes_none(self, tiny_gtfs):
        bus = _rtd_static.routes_by_id()["15"]
        assert bus.route_color is None
        assert bus.type_label == "Bus"

    def test_nextride_url(self, tiny_gtfs):
        a = _rtd_static.routes_by_id()["A"]
        assert a.nextride_url == "https://app.rtd-denver.com/nextride/route/A"

    def test_missing_routes_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rtd_static, "GTFS_DIR", tmp_path / "no_such_dir")
        _rtd_static.clear_caches()
        assert _rtd_static.routes_by_id() == {}


class TestStopsById:
    def test_skips_parent_station(self, tiny_gtfs):
        stops = _rtd_static.stops_by_id()
        assert set(stops) == {"S1", "S2", "S3"}
        assert "SP" not in stops

    def test_stop_record_fields(self, tiny_gtfs):
        s1 = _rtd_static.stops_by_id()["S1"]
        assert s1.stop_code == "26134"
        assert s1.stop_name == "38th & Blake"
        assert s1.stop_desc == "Vehicles Travelling West"
        assert s1.lat == pytest.approx(39.7700)
        assert s1.lon == pytest.approx(-104.9800)

    def test_nextride_url_uses_stop_id_not_code(self, tiny_gtfs):
        s1 = _rtd_static.stops_by_id()["S1"]
        # Per the URL convention: NextRide expects stop_id, not stop_code.
        assert s1.nextride_url == "https://app.rtd-denver.com/nextride/stop/S1"

    def test_display_label_combines_name_and_desc(self, tiny_gtfs):
        s1 = _rtd_static.stops_by_id()["S1"]
        assert s1.display_label == "38th & Blake — Vehicles Travelling West"

    def test_missing_stops_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rtd_static, "GTFS_DIR", tmp_path / "no_such_dir")
        _rtd_static.clear_caches()
        assert _rtd_static.stops_by_id() == {}


class TestStopIdsByName:
    def test_paired_stops_grouped(self, tiny_gtfs):
        pairs = _rtd_static.stop_ids_by_name()
        assert pairs["38th & Blake"] == ["S1", "S2"]

    def test_solo_stop_returns_single_id(self, tiny_gtfs):
        pairs = _rtd_static.stop_ids_by_name()
        assert pairs["Union Station"] == ["S3"]

    def test_index_is_deterministic(self, tiny_gtfs):
        # Run twice and compare — lru_cache should return the same dict object
        # but the sort order should be stable regardless.
        first = _rtd_static.stop_ids_by_name()
        _rtd_static.clear_caches()
        second = _rtd_static.stop_ids_by_name()
        assert first == second


class TestTripsById:
    def test_loads_trips(self, tiny_gtfs):
        trips = _rtd_static.trips_by_id()
        assert set(trips) == {"t_w_in", "t_w_out", "t_15_e"}

    def test_trip_fields(self, tiny_gtfs):
        t = _rtd_static.trips_by_id()["t_w_in"]
        assert t.route_id == "W"
        assert t.headsign == "Union Station"
        assert t.direction_id == 0
        assert t.service_id == "WK"

    def test_missing_trips_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rtd_static, "GTFS_DIR", tmp_path / "no_such_dir")
        _rtd_static.clear_caches()
        assert _rtd_static.trips_by_id() == {}


class TestCompatViews:
    def test_route_id_to_short_name(self, tiny_gtfs):
        assert _rtd_static.route_id_to_short_name() == {
            "W": "W", "15": "15", "A": "A",
        }

    def test_stop_id_to_stop_code(self, tiny_gtfs):
        assert _rtd_static.stop_id_to_stop_code() == {
            "S1": "26134", "S2": "26135", "S3": "26136",
        }


class TestNextrideRouteSlug:
    """Slug derivation for /route/{slug} URLs.

    Verified ground truth from manual browser checks:
      - https://app.rtd-denver.com/nextride/route/W   -> 200
      - https://app.rtd-denver.com/nextride/route/103W -> 404
      - https://app.rtd-denver.com/nextride/route/FREE -> 200
      - https://app.rtd-denver.com/nextride/route/FreeRide -> 404
    """

    def test_rail_shape_id_uses_short_name(self):
        assert _rtd_static.nextride_route_slug("103W", "W") == "W"
        assert _rtd_static.nextride_route_slug("101D", "D") == "D"
        assert _rtd_static.nextride_route_slug("113B", "B") == "B"
        assert _rtd_static.nextride_route_slug("117N", "N") == "N"

    def test_named_circulators_use_route_id(self):
        # FREE/BOND/FMR have non-numeric-prefixed route_ids and short_names
        # like "FreeRide" that NextRide rejects — route_id is the slug.
        assert _rtd_static.nextride_route_slug("FREE", "FreeRide") == "FREE"
        assert _rtd_static.nextride_route_slug("BOND", "Bound") == "BOND"
        assert _rtd_static.nextride_route_slug("FMR", "MetroRide") == "FMR"

    def test_numeric_buses_use_route_id(self):
        # route_id == short_name for these — either would be correct.
        assert _rtd_static.nextride_route_slug("100", "100") == "100"
        assert _rtd_static.nextride_route_slug("FF1", "FF1") == "FF1"

    def test_letter_suffix_buses_use_route_id(self):
        # 104L and 228A also match ^\d+[A-Z]+$, but in their case route_id
        # already equals short_name so the slug result is identical.
        assert _rtd_static.nextride_route_slug("104L", "104L") == "104L"

    def test_fallback_when_short_name_empty(self):
        # Even rail-shape ids fall back to route_id if short_name is empty.
        assert _rtd_static.nextride_route_slug("103W", "") == "103W"

    def test_fallback_when_route_id_empty(self):
        assert _rtd_static.nextride_route_slug("", "W") == "W"


class TestClearCaches:
    def test_clear_caches_invalidates_all(self, tiny_gtfs, monkeypatch):
        # First access caches the loaded data
        assert "W" in _rtd_static.routes_by_id()

        # Move GTFS_DIR to nothing, but cache still has the old result
        monkeypatch.setattr(_rtd_static, "GTFS_DIR", tiny_gtfs.parent / "gone")
        assert "W" in _rtd_static.routes_by_id()  # still cached

        # Clear and re-read — now empty
        _rtd_static.clear_caches()
        assert _rtd_static.routes_by_id() == {}
        assert _rtd_static.stops_by_id() == {}
        assert _rtd_static.stop_ids_by_name() == {}
        assert _rtd_static.trips_by_id() == {}
