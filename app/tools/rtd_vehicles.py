"""rtd_vehicles.py — RTD GTFS-realtime vehicle positions.

Fetches the VehiclePosition.pb feed at query time, filters to vehicles on a
route resolved from the user's query, returns a Pydantic
`VehiclePositionsResult`. Same patterns as `rtd_arrivals.py`: in-memory TTL
cache, error-as-data return contract, no exceptions surfaced.

Route resolution:
  1. Token match against `route_short_name` (case-insensitive). Handles
     "W Line", "the 15", "A line", "FreeRide".
  2. Substring match of `route_long_name` against the normalized query.
     Handles "Colfax", "Limited" variants.
  3. None -> empty result with explanatory error.

Per Tier 3 plan (rtd_tier3_plan memory): a proper LLM-grounded route name
resolver is deferred. The lexical match here covers the common cases.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2
from pydantic import BaseModel, Field

from app.tools import _rtd_static

load_dotenv()
logger = logging.getLogger(__name__)

RTD_VEHICLES_URL = os.getenv(
    "RTD_VEHICLES_URL",
    "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/VehiclePosition.pb",
)
USER_AGENT = os.getenv(
    "RTD_USER_AGENT",
    "blue-cypher-denver-rag (sam.vburgh@gmail.com)",
)
HTTP_TIMEOUT = 15.0
VEHICLES_CACHE_TTL_SECONDS = int(
    os.getenv("RTD_REALTIME_CACHE_TTL_SECONDS", "30")
)
DEFAULT_MAX_VEHICLES = 25
DENVER_TZ = ZoneInfo("America/Denver")

# Per gtfs-realtime VehicleStopStatus enum
_VEHICLE_STATUS_LABELS: dict[int, str] = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}

# Cache the parsed feed (route_id -> list[snapshot tuple]) keyed on URL.
_feed_index_cache: dict[str, tuple[float, dict[str, list]]] = {}

_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]+\b")


# --- pydantic models ---------------------------------------------------------


class RouteRef(BaseModel):
    route_id: str
    short_name: str
    long_name: str
    type_label: str
    nextride_url: str


class VehicleSnapshot(BaseModel):
    vehicle_id: str
    label: str
    route_id: str
    route_short_name: str
    route_long_name: str
    headsign: str
    direction_id: int | None = None
    lat: float
    lon: float
    bearing: float | None = None
    speed_mps: float | None = None
    current_status: str | None = None
    stop_id: str | None = None
    trip_id: str | None = None
    reported_at: datetime
    nextride_route_url: str


class VehiclePositionsResult(BaseModel):
    matched_route: RouteRef | None = None
    vehicles: list[VehicleSnapshot] = Field(default_factory=list)
    has_realtime: bool = False
    fetched_at: datetime
    error: str | None = None


# --- route resolution --------------------------------------------------------


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def resolve_route_id(query: str) -> str | None:
    """Lexical route resolver. Returns route_id or None.

    Strategy: prefer exact short_name token match (case-insensitive); fall
    back to longest long_name substring match. Picks the more specific match
    when multiple candidates tie.
    """
    if not query or not query.strip():
        return None
    routes = _rtd_static.routes_by_id()
    if not routes:
        return None

    tokens = {t.upper() for t in _TOKEN_RE.findall(query)}
    short_hits = [
        rid for rid, rec in routes.items()
        if rec.short_name and rec.short_name.upper() in tokens
    ]
    if len(short_hits) == 1:
        return short_hits[0]
    if len(short_hits) > 1:
        # Tie-break: prefer the route whose long_name also appears in the
        # normalized query. Falls back to first match for determinism.
        qn = _normalize(query)
        for rid in short_hits:
            ln = _normalize(routes[rid].long_name)
            if ln and ln in qn:
                return rid
        return sorted(short_hits)[0]

    # No short_name match — try long_name substring (longest wins)
    qn = _normalize(query)
    best_rid: str | None = None
    best_len = 0
    for rid, rec in routes.items():
        if not rec.long_name:
            continue
        ln = _normalize(rec.long_name)
        if len(ln) <= best_len:
            continue
        if ln in qn:
            best_rid = rid
            best_len = len(ln)
    return best_rid


def _build_route_ref(route_id: str) -> RouteRef | None:
    rec = _rtd_static.routes_by_id().get(route_id)
    if rec is None:
        return None
    return RouteRef(
        route_id=rec.route_id,
        short_name=rec.short_name,
        long_name=rec.long_name,
        type_label=rec.type_label,
        nextride_url=rec.nextride_url,
    )


# --- feed fetch + parse ------------------------------------------------------


def _build_feed_index(feed) -> dict[str, list]:
    """Group VehiclePosition entities by route_id. Each list entry is a tuple
    of the raw fields needed to build a VehicleSnapshot (avoids holding the
    protobuf object in cache)."""
    index: dict[str, list] = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        trip_route_id = v.trip.route_id if v.HasField("trip") and v.trip.HasField("route_id") else ""
        trip_id = v.trip.trip_id if v.HasField("trip") and v.trip.HasField("trip_id") else None
        direction = (
            v.trip.direction_id
            if v.HasField("trip") and v.trip.HasField("direction_id")
            else None
        )

        if not v.HasField("position"):
            continue
        lat = v.position.latitude
        lon = v.position.longitude
        bearing = v.position.bearing if v.position.HasField("bearing") else None
        speed = v.position.speed if v.position.HasField("speed") else None

        ts = v.timestamp if v.HasField("timestamp") else 0
        status_code = v.current_status if v.HasField("current_status") else None
        stop_id = v.stop_id if v.HasField("stop_id") else None
        vehicle_id = v.vehicle.id if v.HasField("vehicle") and v.vehicle.id else entity.id
        label = v.vehicle.label if v.HasField("vehicle") and v.vehicle.label else vehicle_id

        if not trip_route_id:
            continue

        index.setdefault(trip_route_id, []).append(
            (
                vehicle_id, label, trip_id, direction,
                lat, lon, bearing, speed,
                status_code, stop_id, ts,
            )
        )
    return index


async def _fetch_and_index_feed(
    url: str | None = None,
) -> tuple[dict[str, list] | None, str | None]:
    """30s TTL cache around the protobuf fetch + parse + index. Failures are
    not cached."""
    target_url = url or RTD_VEHICLES_URL
    now = time.time()
    cached = _feed_index_cache.get(target_url)
    if cached is not None and now - cached[0] < VEHICLES_CACHE_TTL_SECONDS:
        return cached[1], None

    headers = {"User-Agent": USER_AGENT, "Accept": "application/x-protobuf"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(target_url)
            resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
    except Exception as exc:
        logger.warning("RTD vehicle positions fetch failed: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"

    index = _build_feed_index(feed)
    _feed_index_cache[target_url] = (now, index)
    return index, None


# --- snapshot construction --------------------------------------------------


def _build_snapshot(entry: tuple, route_rec: _rtd_static.RouteRecord) -> VehicleSnapshot:
    (
        vehicle_id, label, trip_id, direction_desc,
        lat, lon, bearing, speed,
        status_code, stop_id, ts,
    ) = entry

    headsign = ""
    direction = direction_desc
    if trip_id:
        trip_record = _rtd_static.trips_by_id().get(trip_id)
        if trip_record:
            headsign = trip_record.headsign
            if trip_record.direction_id is not None:
                direction = trip_record.direction_id

    reported_at = (
        datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(DENVER_TZ)
        if ts else datetime.now(DENVER_TZ)
    )
    status_label = _VEHICLE_STATUS_LABELS.get(status_code) if status_code is not None else None

    return VehicleSnapshot(
        vehicle_id=vehicle_id,
        label=label,
        route_id=route_rec.route_id,
        route_short_name=route_rec.short_name,
        route_long_name=route_rec.long_name,
        headsign=headsign,
        direction_id=direction,
        lat=lat,
        lon=lon,
        bearing=bearing,
        speed_mps=speed,
        current_status=status_label,
        stop_id=stop_id,
        trip_id=trip_id,
        reported_at=reported_at,
        nextride_route_url=route_rec.nextride_url,
    )


# --- public API --------------------------------------------------------------


async def get_vehicle_positions_for_query(
    query: str,
    max_results: int = DEFAULT_MAX_VEHICLES,
    url: str | None = None,
) -> VehiclePositionsResult:
    """Resolve a route from `query`, fetch live vehicle positions, return a
    Pydantic result. Errors are returned as data via `error`; never raises."""
    fetched_at = datetime.now(timezone.utc)
    route_id = resolve_route_id(query)
    if route_id is None:
        return VehiclePositionsResult(
            matched_route=None,
            vehicles=[],
            has_realtime=False,
            fetched_at=fetched_at,
            error=(
                "Could not resolve an RTD route from the query. Mention a "
                "route name like 'W Line', 'A Line', 'Route 15', or 'FreeRide'."
            ),
        )

    route_ref = _build_route_ref(route_id)
    route_rec = _rtd_static.routes_by_id().get(route_id)
    if route_rec is None or route_ref is None:
        # Should not happen — resolve_route_id only returns ids in routes_by_id
        return VehiclePositionsResult(
            matched_route=None,
            vehicles=[],
            has_realtime=False,
            fetched_at=fetched_at,
            error=f"Resolved route_id {route_id!r} missing from static GTFS",
        )

    index, fetch_error = await _fetch_and_index_feed(url=url)
    if fetch_error is not None or index is None:
        return VehiclePositionsResult(
            matched_route=route_ref,
            vehicles=[],
            has_realtime=False,
            fetched_at=fetched_at,
            error=fetch_error or "Unknown fetch error",
        )

    entries = index.get(route_id, [])
    snapshots = [_build_snapshot(e, route_rec) for e in entries]
    # Sort newest-reported first so a UI map renders the freshest blip on top.
    snapshots.sort(key=lambda s: s.reported_at, reverse=True)
    capped = snapshots[:max_results]

    return VehiclePositionsResult(
        matched_route=route_ref,
        vehicles=capped,
        has_realtime=True,
        fetched_at=fetched_at,
        error=None,
    )


def clear_caches() -> None:
    """Clear in-process caches. Useful for tests."""
    _feed_index_cache.clear()
    _rtd_static.clear_caches()
