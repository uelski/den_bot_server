"""Tests for app.tools.rtd_arrivals.

Pattern matches test_rtd_alerts: build synthetic FeedMessage protobuf in
memory, patch httpx.AsyncClient. Static GTFS uses the same tiny CSV fixture
shape as test_rtd_static.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from app.tools import _rtd_static, rtd_arrivals as arrivals_module
from app.tools.rtd_arrivals import (
    DENVER_TZ,
    Arrival,
    ArrivalsResult,
    clear_caches,
    get_arrivals_for_query,
    resolve_stop_ids,
)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def tiny_gtfs(tmp_path: Path, monkeypatch) -> Path:
    """Mirrors the fixture in test_rtd_static. Two paired stops at one
    intersection plus one solo stop, three trips on two routes."""
    gtfs_dir = tmp_path / "rtd_gtfs"
    gtfs_dir.mkdir()

    (gtfs_dir / "stops.txt").write_text(textwrap.dedent("""\
        stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,location_type,parent_station,wheelchair_boarding
        S1,26134,38th & Blake,Vehicles Travelling West,39.7700,-104.9800,0,,1
        S2,26135,38th & Blake,Vehicles Travelling East,39.7701,-104.9799,0,,1
        S3,26136,Union Station,Track 1,39.7530,-105.0000,0,,1
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
        A,WK,t_a_apt,Denver Airport,0
    """))

    monkeypatch.setattr(_rtd_static, "GTFS_DIR", gtfs_dir)
    clear_caches()
    yield gtfs_dir
    clear_caches()


def _build_trip_update_feed(*entries) -> bytes:
    """Build a serialized FeedMessage containing trip updates.

    Each `entries` item is (entity_id, kwargs). Recognized kwargs:
      trip_id (str), route_id (str, optional), direction_id (int, optional)
      stop_times: list of dicts with keys: stop_id, arrival_time, departure_time,
                  delay, schedule_relationship.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1714000000

    for entity_id, kwargs in entries:
        entity = feed.entity.add()
        entity.id = entity_id
        tu = entity.trip_update
        tu.trip.trip_id = kwargs.get("trip_id", entity_id)
        if "route_id" in kwargs:
            tu.trip.route_id = kwargs["route_id"]
        if "direction_id" in kwargs:
            tu.trip.direction_id = kwargs["direction_id"]

        for stu_kwargs in kwargs.get("stop_times", []):
            stu = tu.stop_time_update.add()
            stu.stop_id = stu_kwargs["stop_id"]
            if "arrival_time" in stu_kwargs:
                stu.arrival.time = stu_kwargs["arrival_time"]
            if "arrival_delay" in stu_kwargs:
                stu.arrival.delay = stu_kwargs["arrival_delay"]
            if "departure_time" in stu_kwargs:
                stu.departure.time = stu_kwargs["departure_time"]
            if "departure_delay" in stu_kwargs:
                stu.departure.delay = stu_kwargs["departure_delay"]
            if "schedule_relationship" in stu_kwargs:
                stu.schedule_relationship = stu_kwargs["schedule_relationship"]

    return feed.SerializeToString()


def _patch_httpx(content: bytes, status_code: int = 200):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = content
    if status_code != 200:
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"{status_code}", request=MagicMock(), response=response
            )
        )
    else:
        response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    return patch.object(httpx, "AsyncClient", return_value=mock_client)


def _ts_minutes_from_now(minutes: float, base: datetime | None = None) -> int:
    """Unix epoch for `minutes` after the given base (default: now in Denver)."""
    if base is None:
        base = datetime.now(DENVER_TZ)
    return int((base + timedelta(minutes=minutes)).timestamp())


# --- stop resolution tests --------------------------------------------------


class TestResolveByStopCode:
    def test_finds_known_stop_code(self, tiny_gtfs):
        stop_ids, method = resolve_stop_ids("anything happening at stop 26134?")
        assert stop_ids == ["S1"]
        assert method == "stop_code"

    def test_unknown_stop_code_falls_through(self, tiny_gtfs):
        # 99999 is not in the fixture; will fall through to name/neighborhood
        # resolvers, which also won't match the bare numeric query
        stop_ids, method = resolve_stop_ids("stop 99999")
        assert stop_ids == []
        assert method == "none"

    def test_short_numeric_token_ignored(self, tiny_gtfs):
        # 3-digit numbers are not stop codes (regex requires 4-5)
        stop_ids, method = resolve_stop_ids("stop 123")
        assert stop_ids == []
        assert method == "none"


class TestResolveByStopName:
    def test_finds_paired_stops_by_name(self, tiny_gtfs):
        stop_ids, method = resolve_stop_ids("when's the next train at 38th & Blake")
        assert sorted(stop_ids) == ["S1", "S2"]
        assert method == "stop_name"

    def test_and_normalization_matches_ampersand(self, tiny_gtfs):
        stop_ids, method = resolve_stop_ids("next train at 38th and Blake")
        assert sorted(stop_ids) == ["S1", "S2"]
        assert method == "stop_name"

    def test_solo_stop_returns_single_id(self, tiny_gtfs):
        stop_ids, method = resolve_stop_ids("when's the next train at Union Station")
        assert stop_ids == ["S3"]
        assert method == "stop_name"

    def test_no_match_returns_empty(self, tiny_gtfs):
        stop_ids, method = resolve_stop_ids("the spaceship terminal")
        assert stop_ids == []
        assert method == "none"


class TestResolveByNeighborhood:
    def test_neighborhood_resolved_to_nearest_paired_stops(self, tiny_gtfs):
        # Mock the resolver and the qdrant centroid lookup to return a centroid
        # very close to S1/S2 so haversine picks them
        mock_resolved = MagicMock()
        mock_resolved.name = "Five Points"
        with patch.object(arrivals_module, "resolve", return_value=mock_resolved), \
             patch.object(
                 arrivals_module, "_get_neighborhood_centroid",
                 return_value=(39.7700, -104.9800),
             ):
            stop_ids, method = resolve_stop_ids("near five points")
        assert sorted(stop_ids) == ["S1", "S2"]
        assert method == "neighborhood_centroid"

    def test_centroid_too_far_returns_empty(self, tiny_gtfs):
        mock_resolved = MagicMock()
        mock_resolved.name = "Far Far Away"
        with patch.object(arrivals_module, "resolve", return_value=mock_resolved), \
             patch.object(
                 arrivals_module, "_get_neighborhood_centroid",
                 return_value=(0.0, 0.0),  # off-globe relative to Denver fixtures
             ):
            stop_ids, method = resolve_stop_ids("far far away")
        assert stop_ids == []
        assert method == "none"


# --- end-to-end happy path --------------------------------------------------


class TestGetArrivalsHappyPath:
    @pytest.mark.asyncio
    async def test_returns_arrivals_for_resolved_stop(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5),
                     "arrival_delay": 60},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")

        assert result.error is None
        assert result.has_realtime is True
        assert result.resolution_method == "stop_name"
        assert len(result.matched_stops) == 1
        assert result.matched_stops[0].stop_id == "S3"
        assert len(result.arrivals) == 1
        a = result.arrivals[0]
        assert a.trip_id == "t_w_in"
        assert a.route_id == "W"
        assert a.route_short_name == "W"
        assert a.headsign == "Union Station"
        assert a.delay_seconds == 60
        assert 0 <= a.minutes_until <= 6
        assert a.nextride_stop_url == "https://app.rtd-denver.com/nextride/stop/S3"
        assert a.nextride_route_url == "https://app.rtd-denver.com/nextride/route/W"

    @pytest.mark.asyncio
    async def test_paired_directions_both_returned(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S1", "arrival_time": _ts_minutes_from_now(3)},
                ],
            }),
            ("e2", {
                "trip_id": "t_w_out",
                "stop_times": [
                    {"stop_id": "S2", "arrival_time": _ts_minutes_from_now(7)},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at 38th & Blake")

        # Both paired stops should be in matched_stops
        assert {s.stop_id for s in result.matched_stops} == {"S1", "S2"}
        assert {a.stop_id for a in result.arrivals} == {"S1", "S2"}
        # Sorted by predicted_time ascending
        assert result.arrivals[0].stop_id == "S1"
        assert result.arrivals[1].stop_id == "S2"

    @pytest.mark.asyncio
    async def test_window_filter_excludes_far_future(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("near", {
                "trip_id": "t_w_in",
                "stop_times": [{"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)}],
            }),
            ("far", {
                "trip_id": "t_w_out",
                "stop_times": [{"stop_id": "S3", "arrival_time": _ts_minutes_from_now(90)}],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")

        assert len(result.arrivals) == 1
        assert result.arrivals[0].trip_id == "t_w_in"

    @pytest.mark.asyncio
    async def test_max_results_caps_arrivals(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            *[
                (f"e{i}", {
                    "trip_id": "t_w_in",
                    "stop_times": [
                        {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5 + i)},
                    ],
                })
                for i in range(8)
            ]
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query(
                "next train at Union Station", max_results=3
            )
        assert len(result.arrivals) == 3

    @pytest.mark.asyncio
    async def test_past_arrivals_excluded(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("past", {
                "trip_id": "t_w_in",
                "stop_times": [{"stop_id": "S3", "arrival_time": _ts_minutes_from_now(-5)}],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.arrivals == []


# --- feed parse edge cases --------------------------------------------------


class TestFeedParsing:
    @pytest.mark.asyncio
    async def test_skipped_relationship_excluded(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5),
                     "schedule_relationship": 1},  # SKIPPED
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.arrivals == []

    @pytest.mark.asyncio
    async def test_no_data_relationship_excluded(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5),
                     "schedule_relationship": 2},  # NO_DATA
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.arrivals == []

    @pytest.mark.asyncio
    async def test_departure_time_fallback_when_no_arrival(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "departure_time": _ts_minutes_from_now(4)},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert len(result.arrivals) == 1

    @pytest.mark.asyncio
    async def test_entry_with_no_time_excluded(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_delay": 30},  # delay only, no time
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.arrivals == []

    @pytest.mark.asyncio
    async def test_unknown_trip_uses_descriptor_route_id(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "trip_not_in_static",
                "route_id": "A",  # in static GTFS even though trip isn't
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")

        assert len(result.arrivals) == 1
        a = result.arrivals[0]
        assert a.route_id == "A"
        assert a.route_short_name == "A"
        # No trip record so headsign falls back to empty
        assert a.headsign == ""

    @pytest.mark.asyncio
    async def test_unknown_trip_no_descriptor_skipped(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "trip_not_in_static",
                # no route_id descriptor — can't infer the route, skip the row
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.arrivals == []


# --- error paths ------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_unresolved_stop_returns_error(self, tiny_gtfs):
        result = await get_arrivals_for_query("the spaceship terminal")
        assert result.error is not None
        assert result.arrivals == []
        assert result.matched_stops == []
        assert result.resolution_method == "none"

    @pytest.mark.asyncio
    async def test_http_error_returns_error_field(self, tiny_gtfs):
        with _patch_httpx(b"", status_code=500):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.error is not None
        assert result.has_realtime is False
        # Stop was resolved, so matched_stops is populated even on fetch failure
        assert len(result.matched_stops) == 1

    @pytest.mark.asyncio
    async def test_garbled_protobuf_returns_error(self, tiny_gtfs):
        with _patch_httpx(b"not a real protobuf"):
            result = await get_arrivals_for_query("next train at Union Station")
        assert result.error is not None
        assert result.arrivals == []


# --- caching ----------------------------------------------------------------


class TestCaching:
    @pytest.mark.asyncio
    async def test_repeat_call_within_ttl_caches(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)},
                ],
            }),
        )
        with _patch_httpx(feed):
            await get_arrivals_for_query("next train at Union Station")
            await get_arrivals_for_query("next train at Union Station")
        assert len(arrivals_module._feed_index_cache) == 1

    @pytest.mark.asyncio
    async def test_failed_fetches_not_cached(self, tiny_gtfs):
        with _patch_httpx(b"", status_code=500):
            await get_arrivals_for_query("next train at Union Station")
        assert arrivals_module._feed_index_cache == {}

    @pytest.mark.asyncio
    async def test_clear_caches_evicts_entries(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)},
                ],
            }),
        )
        with _patch_httpx(feed):
            await get_arrivals_for_query("next train at Union Station")
        assert len(arrivals_module._feed_index_cache) == 1
        clear_caches()
        assert arrivals_module._feed_index_cache == {}


# --- result model -----------------------------------------------------------


class TestResultModel:
    @pytest.mark.asyncio
    async def test_serializes_to_json(self, tiny_gtfs):
        feed = _build_trip_update_feed(
            ("e1", {
                "trip_id": "t_w_in",
                "stop_times": [
                    {"stop_id": "S3", "arrival_time": _ts_minutes_from_now(5)},
                ],
            }),
        )
        with _patch_httpx(feed):
            result = await get_arrivals_for_query("next train at Union Station")
        data = result.model_dump(mode="json")
        assert isinstance(data["arrivals"], list)
        assert isinstance(data["matched_stops"], list)
        assert isinstance(data["fetched_at"], str)
        assert data["error"] is None
        assert data["has_realtime"] is True

    def test_arrivals_result_default_lists(self):
        result = ArrivalsResult(fetched_at=datetime.now(timezone.utc))
        assert result.arrivals == []
        assert result.matched_stops == []
        assert result.has_realtime is False
