"""Static GTFS lookups shared across RTD tools.

Reads stops.txt, routes.txt, and trips.txt from data/rtd_gtfs/ once per process
and exposes lazy accessors for the various id/name maps the live-feed tools
(rtd_alerts, rtd_arrivals, rtd_vehicles) need to enrich realtime entities with
human-readable metadata.

All accessors return empty dicts when the underlying GTFS file is missing —
callers should treat that as "filtering disabled" rather than fatal.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GTFS_DIR = BASE_DIR / "data" / "rtd_gtfs"

NEXTRIDE_STOP_URL_TEMPLATE = "https://app.rtd-denver.com/nextride/stop/{stop_id}"
NEXTRIDE_ROUTE_URL_TEMPLATE = "https://app.rtd-denver.com/nextride/route/{route_id}"

# Matches route_ids RTD prefixes with a network-internal numeric ID for rail
# (e.g. "103W", "101D", "113B"). These are the only routes whose route_id
# differs from the slug NextRide accepts in /route/{slug}.
_RAIL_ROUTE_ID_RE = re.compile(r"^\d+[A-Z]+$")


def nextride_route_slug(route_id: str, short_name: str) -> str:
    """Return the slug NextRide expects for the given route.

    RTD's GTFS uses two different identifier conventions and NextRide accepts
    different ones for each:
      - Rail / commuter rail: route_id is "{digits}{letters}" (e.g. "103W"),
        and NextRide expects the bare letter (route_short_name "W").
      - Numeric buses (100, 105, ...), Flatiron Flyer (FF1, FF2, ...), and the
        three named circulators (FREE, BOND, FMR): route_id is already the
        accepted slug — short_name is the friendlier label (FreeRide, Bound,
        MetroRide) but does NOT work as a URL slug.

    Falls back to whichever of the two is non-empty if its preferred field
    is missing — defensive against malformed GTFS rows."""
    if _RAIL_ROUTE_ID_RE.match(route_id) and short_name:
        return short_name
    return route_id or short_name

# GTFS route_type enum -> human-readable label.
ROUTE_TYPE_LABELS: dict[int, str] = {
    0: "Light Rail",
    1: "Subway",
    2: "Commuter Rail",
    3: "Bus",
    4: "Ferry",
    5: "Cable Tram",
    6: "Aerial Lift",
    7: "Funicular",
    11: "Trolleybus",
    12: "Monorail",
}


@dataclass(frozen=True)
class RouteRecord:
    route_id: str
    short_name: str
    long_name: str
    route_type: int
    route_color: str | None

    @property
    def type_label(self) -> str:
        return ROUTE_TYPE_LABELS.get(self.route_type, "Transit")

    @property
    def nextride_url(self) -> str:
        """RTD NextRide deep link, using the slug rule encoded in
        `nextride_route_slug` (rail uses short_name, everything else uses
        route_id)."""
        return NEXTRIDE_ROUTE_URL_TEMPLATE.format(
            route_id=nextride_route_slug(self.route_id, self.short_name)
        )


@dataclass(frozen=True)
class StopRecord:
    stop_id: str
    stop_code: str
    stop_name: str
    stop_desc: str
    lat: float
    lon: float

    @property
    def nextride_url(self) -> str:
        return NEXTRIDE_STOP_URL_TEMPLATE.format(stop_id=self.stop_id)

    @property
    def display_label(self) -> str:
        """`stop_name — stop_desc` when both present, else whichever exists."""
        if self.stop_name and self.stop_desc:
            return f"{self.stop_name} — {self.stop_desc}"
        return self.stop_name or self.stop_desc or self.stop_id


@dataclass(frozen=True)
class TripRecord:
    trip_id: str
    route_id: str
    headsign: str
    direction_id: int | None
    service_id: str


@lru_cache(maxsize=1)
def routes_by_id() -> dict[str, RouteRecord]:
    """Return {route_id: RouteRecord}. Empty if routes.txt missing."""
    path = GTFS_DIR / "routes.txt"
    if not path.exists():
        logger.warning("routes.txt not found at %s — RTD route lookups disabled", path)
        return {}
    out: dict[str, RouteRecord] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            if not rid:
                continue
            try:
                rtype = int((row.get("route_type") or "0").strip())
            except ValueError:
                rtype = 0
            color = (row.get("route_color") or "").strip() or None
            out[rid] = RouteRecord(
                route_id=rid,
                short_name=(row.get("route_short_name") or "").strip(),
                long_name=(row.get("route_long_name") or "").strip(),
                route_type=rtype,
                route_color=color,
            )
    logger.info("loaded %d routes from %s", len(out), path.name)
    return out


@lru_cache(maxsize=1)
def stops_by_id() -> dict[str, StopRecord]:
    """Return {stop_id: StopRecord}. Skips stops with location_type != 0
    (parent stations, entrances, generic nodes — not useful for "where to
    catch a bus" lookups). Empty if stops.txt missing."""
    path = GTFS_DIR / "stops.txt"
    if not path.exists():
        logger.warning("stops.txt not found at %s — RTD stop lookups disabled", path)
        return {}
    out: dict[str, StopRecord] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("stop_id") or "").strip()
            if not sid:
                continue
            location_type = (row.get("location_type") or "0").strip() or "0"
            if location_type not in ("", "0"):
                continue
            try:
                lat = float(row.get("stop_lat") or "0")
                lon = float(row.get("stop_lon") or "0")
            except ValueError:
                continue
            out[sid] = StopRecord(
                stop_id=sid,
                stop_code=(row.get("stop_code") or "").strip() or sid,
                stop_name=(row.get("stop_name") or "").strip(),
                stop_desc=(row.get("stop_desc") or "").strip(),
                lat=lat,
                lon=lon,
            )
    logger.info("loaded %d stops from %s", len(out), path.name)
    return out


@lru_cache(maxsize=1)
def stop_ids_by_name() -> dict[str, list[str]]:
    """Return {stop_name: [stop_id, ...]} — the direction-pairing index.

    Stops sharing a stop_name are the same intersection in different
    directions. Empty list values are filtered out; ids are sorted for
    deterministic output."""
    out: dict[str, list[str]] = {}
    for stop in stops_by_id().values():
        if not stop.stop_name:
            continue
        out.setdefault(stop.stop_name, []).append(stop.stop_id)
    for name in out:
        out[name].sort()
    return out


@lru_cache(maxsize=1)
def trips_by_id() -> dict[str, TripRecord]:
    """Return {trip_id: TripRecord}. Empty if trips.txt missing.

    Used to enrich a realtime feed's trip_id with route_id and human-friendly
    headsign/direction_id."""
    path = GTFS_DIR / "trips.txt"
    if not path.exists():
        logger.warning("trips.txt not found at %s — RTD trip lookups disabled", path)
        return {}
    out: dict[str, TripRecord] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = (row.get("trip_id") or "").strip()
            if not tid:
                continue
            direction_raw = (row.get("direction_id") or "").strip()
            try:
                direction = int(direction_raw) if direction_raw else None
            except ValueError:
                direction = None
            out[tid] = TripRecord(
                trip_id=tid,
                route_id=(row.get("route_id") or "").strip(),
                headsign=(row.get("trip_headsign") or "").strip(),
                direction_id=direction,
                service_id=(row.get("service_id") or "").strip(),
            )
    logger.info("loaded %d trips from %s", len(out), path.name)
    return out


@lru_cache(maxsize=1)
def route_id_to_short_name() -> dict[str, str]:
    """Compatibility view used by rtd_alerts query filtering.
    {route_id: route_short_name} — entries with empty short_name are dropped."""
    return {
        rid: rec.short_name
        for rid, rec in routes_by_id().items()
        if rec.short_name
    }


@lru_cache(maxsize=1)
def stop_id_to_stop_code() -> dict[str, str]:
    """Compatibility view used by rtd_alerts query filtering.
    {stop_id: stop_code} — entries with empty stop_code are dropped."""
    return {
        sid: rec.stop_code
        for sid, rec in stops_by_id().items()
        if rec.stop_code
    }


def clear_caches() -> None:
    """Clear all in-process GTFS caches. Useful for tests and after re-extracting
    data/rtd_gtfs/."""
    for fn in (
        routes_by_id,
        stops_by_id,
        stop_ids_by_name,
        trips_by_id,
        route_id_to_short_name,
        stop_id_to_stop_code,
    ):
        fn.cache_clear()
