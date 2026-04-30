"""Tests for app.tools.rtd_vehicles.

Synthetic VehiclePosition feeds in-memory + tiny GTFS fixture. Same patterns
as test_rtd_arrivals / test_rtd_alerts.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from app.tools import _rtd_static, rtd_vehicles as vehicles_module
from app.tools.rtd_vehicles import (
    VehiclePositionsResult,
    clear_caches,
    get_vehicle_positions_for_query,
    resolve_route_id,
)


@pytest.fixture
def tiny_gtfs(tmp_path: Path, monkeypatch) -> Path:
    gtfs_dir = tmp_path / "rtd_gtfs"
    gtfs_dir.mkdir()
    (gtfs_dir / "stops.txt").write_text(textwrap.dedent("""\
        stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,location_type,parent_station,wheelchair_boarding
        S3,26136,Union Station,Track 1,39.7530,-105.0000,0,,1
    """))
    (gtfs_dir / "routes.txt").write_text(textwrap.dedent("""\
        route_id,route_short_name,route_long_name,route_type,route_color
        W,W,W Line,0,FFCC00
        15,15,Colfax,3,
        A,A,A Line,2,1B5E20
        FreeRide,FreeRide,Free MallRide,3,
    """))
    (gtfs_dir / "trips.txt").write_text(textwrap.dedent("""\
        route_id,service_id,trip_id,trip_headsign,direction_id
        W,WK,t_w_in,Union Station,0
        15,WK,t_15_e,East Colfax,0
        A,WK,t_a_apt,Denver Airport,0
    """))

    monkeypatch.setattr(_rtd_static, "GTFS_DIR", gtfs_dir)
    clear_caches()
    yield gtfs_dir
    clear_caches()


def _build_vehicle_feed(*entries) -> bytes:
    """Each entries item: (entity_id, kwargs).
    Recognized kwargs: trip_id, route_id, direction_id, lat, lon, bearing,
    speed, current_status (int), stop_id, vehicle_id, label, timestamp.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1714000000

    for entity_id, kw in entries:
        ent = feed.entity.add()
        ent.id = entity_id
        v = ent.vehicle
        if "route_id" in kw:
            v.trip.route_id = kw["route_id"]
        if "trip_id" in kw:
            v.trip.trip_id = kw["trip_id"]
        if "direction_id" in kw:
            v.trip.direction_id = kw["direction_id"]
        if "lat" in kw or "lon" in kw:
            v.position.latitude = kw.get("lat", 0.0)
            v.position.longitude = kw.get("lon", 0.0)
            if "bearing" in kw:
                v.position.bearing = kw["bearing"]
            if "speed" in kw:
                v.position.speed = kw["speed"]
        if "timestamp" in kw:
            v.timestamp = kw["timestamp"]
        if "current_status" in kw:
            v.current_status = kw["current_status"]
        if "stop_id" in kw:
            v.stop_id = kw["stop_id"]
        if "vehicle_id" in kw:
            v.vehicle.id = kw["vehicle_id"]
        if "label" in kw:
            v.vehicle.label = kw["label"]
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


# --- route resolution -------------------------------------------------------


class TestResolveRouteId:
    def test_short_name_token_match(self, tiny_gtfs):
        assert resolve_route_id("any updates on the W Line?") == "W"

    def test_short_name_lowercase_match(self, tiny_gtfs):
        assert resolve_route_id("where is the w line right now") == "W"

    def test_numeric_short_name_match(self, tiny_gtfs):
        assert resolve_route_id("where is the 15 bus") == "15"

    def test_freeride_short_name_match(self, tiny_gtfs):
        assert resolve_route_id("FreeRide location?") == "FreeRide"

    def test_long_name_substring_fallback(self, tiny_gtfs):
        # Query mentions "Colfax" but not "15" — falls back to long_name match
        assert resolve_route_id("where are the Colfax buses") == "15"

    def test_no_match_returns_none(self, tiny_gtfs):
        assert resolve_route_id("the spaceship line") is None

    def test_empty_query_returns_none(self, tiny_gtfs):
        assert resolve_route_id("") is None


# --- end-to-end happy path --------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_vehicles_for_resolved_route(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v1", {
                "route_id": "W",
                "trip_id": "t_w_in",
                "lat": 39.75, "lon": -105.0,
                "bearing": 90.0, "speed": 12.5,
                "vehicle_id": "1234", "label": "1234",
                "timestamp": 1714000000,
                "current_status": 2,  # IN_TRANSIT_TO
            }),
            ("v2", {
                "route_id": "15",  # different route, should be filtered out
                "trip_id": "t_15_e",
                "lat": 39.74, "lon": -105.0,
                "vehicle_id": "5678",
            }),
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("where is the W Line right now")

        assert result.error is None
        assert result.has_realtime is True
        assert result.matched_route.route_id == "W"
        assert result.matched_route.short_name == "W"
        assert result.matched_route.nextride_url == "https://app.rtd-denver.com/nextride/route/W"
        assert len(result.vehicles) == 1
        v = result.vehicles[0]
        assert v.route_id == "W"
        assert v.headsign == "Union Station"
        assert v.lat == pytest.approx(39.75)
        assert v.lon == pytest.approx(-105.0)
        assert v.bearing == pytest.approx(90.0)
        assert v.speed_mps == pytest.approx(12.5)
        assert v.current_status == "IN_TRANSIT_TO"
        assert v.vehicle_id == "1234"
        assert v.nextride_route_url == "https://app.rtd-denver.com/nextride/route/W"

    @pytest.mark.asyncio
    async def test_sorted_newest_first(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v_old", {
                "route_id": "W", "lat": 39.75, "lon": -105.0,
                "vehicle_id": "old", "timestamp": 1714000000,
            }),
            ("v_new", {
                "route_id": "W", "lat": 39.76, "lon": -105.0,
                "vehicle_id": "new", "timestamp": 1714000100,
            }),
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("W Line vehicles")

        assert [v.vehicle_id for v in result.vehicles] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_max_results_caps_vehicles(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            *[
                (f"v{i}", {
                    "route_id": "W", "lat": 39.75 + i * 0.001, "lon": -105.0,
                    "vehicle_id": f"v{i}", "timestamp": 1714000000 + i,
                })
                for i in range(10)
            ]
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("W Line", max_results=3)
        assert len(result.vehicles) == 3

    @pytest.mark.asyncio
    async def test_entity_without_position_skipped(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("nopos", {"route_id": "W", "vehicle_id": "ghost"}),
            ("withpos", {
                "route_id": "W", "lat": 39.75, "lon": -105.0,
                "vehicle_id": "good",
            }),
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("W Line")
        assert {v.vehicle_id for v in result.vehicles} == {"good"}

    @pytest.mark.asyncio
    async def test_status_code_decoded(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v1", {
                "route_id": "W", "lat": 39.75, "lon": -105.0,
                "current_status": 1, "stop_id": "S3", "vehicle_id": "v1",
            }),
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("W Line")
        assert result.vehicles[0].current_status == "STOPPED_AT"
        assert result.vehicles[0].stop_id == "S3"


# --- error paths ------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_unresolved_route_returns_error(self, tiny_gtfs):
        result = await get_vehicle_positions_for_query("the spaceship line")
        assert result.error is not None
        assert result.matched_route is None
        assert result.vehicles == []

    @pytest.mark.asyncio
    async def test_http_error_returns_error_field(self, tiny_gtfs):
        with _patch_httpx(b"", status_code=500):
            result = await get_vehicle_positions_for_query("W Line")
        assert result.error is not None
        assert result.has_realtime is False
        # Route was resolved, so matched_route is populated even on fetch fail
        assert result.matched_route.route_id == "W"

    @pytest.mark.asyncio
    async def test_garbled_protobuf_returns_error(self, tiny_gtfs):
        with _patch_httpx(b"not protobuf"):
            result = await get_vehicle_positions_for_query("W Line")
        assert result.error is not None
        assert result.vehicles == []


# --- caching ----------------------------------------------------------------


class TestCaching:
    @pytest.mark.asyncio
    async def test_repeat_call_within_ttl_caches(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v1", {"route_id": "W", "lat": 39.75, "lon": -105.0, "vehicle_id": "v1"}),
        )
        with _patch_httpx(feed):
            await get_vehicle_positions_for_query("W Line")
            await get_vehicle_positions_for_query("W Line")
        assert len(vehicles_module._feed_index_cache) == 1

    @pytest.mark.asyncio
    async def test_failed_fetches_not_cached(self, tiny_gtfs):
        with _patch_httpx(b"", status_code=500):
            await get_vehicle_positions_for_query("W Line")
        assert vehicles_module._feed_index_cache == {}

    @pytest.mark.asyncio
    async def test_clear_caches_evicts_entries(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v1", {"route_id": "W", "lat": 39.75, "lon": -105.0, "vehicle_id": "v1"}),
        )
        with _patch_httpx(feed):
            await get_vehicle_positions_for_query("W Line")
        assert len(vehicles_module._feed_index_cache) == 1
        clear_caches()
        assert vehicles_module._feed_index_cache == {}


# --- result model -----------------------------------------------------------


class TestResultModel:
    @pytest.mark.asyncio
    async def test_serializes_to_json(self, tiny_gtfs):
        feed = _build_vehicle_feed(
            ("v1", {"route_id": "W", "lat": 39.75, "lon": -105.0, "vehicle_id": "v1"}),
        )
        with _patch_httpx(feed):
            result = await get_vehicle_positions_for_query("W Line")
        data = result.model_dump(mode="json")
        assert isinstance(data["vehicles"], list)
        assert isinstance(data["fetched_at"], str)
        assert data["error"] is None
        assert data["has_realtime"] is True
        assert data["matched_route"]["short_name"] == "W"

    def test_default_lists(self):
        r = VehiclePositionsResult(fetched_at=datetime.now(timezone.utc))
        assert r.vehicles == []
        assert r.matched_route is None
        assert r.has_realtime is False
