"""rtd_arrivals.py — RTD GTFS-realtime live arrivals.

Fetches the TripUpdate.pb feed at query time, joins it with the static GTFS
metadata loaded by `_rtd_static`, and answers "when is the next train/bus at
this stop?" Mirrors the patterns used by `weather.py` and `rtd_alerts.py`:
in-memory TTL cache, error-as-data return contract, no exceptions surfaced.

Stop resolution cascade:
  1. Stop code in the query (4-5 digit numeric token) -> single stop_id.
  2. Stop name substring match -> all stop_ids sharing that name (paired
     directions).
  3. Neighborhood resolved via app.neighborhoods.resolver -> centroid lookup
     in Qdrant -> nearest stop by haversine, paired with its sibling.
  4. None -> empty result with an explanatory error.

Per design decision #2 (rtd_tier3_plan memory): we trust the realtime feed
alone and do NOT walk static stop_times.txt / calendar.txt. Trips with no
realtime entry simply don't appear in the result. The tradeoff is accepted.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.neighborhoods.resolver import ResolvedNeighborhood, resolve
from app.tools import _rtd_static

load_dotenv()
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

RTD_TRIPUPDATES_URL = os.getenv(
    "RTD_TRIPUPDATES_URL",
    "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/TripUpdate.pb",
)
USER_AGENT = os.getenv(
    "RTD_USER_AGENT",
    "blue-cypher-denver-rag (sam.vburgh@gmail.com)",
)
HTTP_TIMEOUT = 15.0
ARRIVALS_CACHE_TTL_SECONDS = int(
    os.getenv("RTD_REALTIME_CACHE_TTL_SECONDS", "30")
)
DEFAULT_WINDOW_MINUTES = 30
DEFAULT_MAX_RESULTS = 5
NEAREST_STOP_MAX_KM = 1.5

DENVER_TZ = ZoneInfo("America/Denver")
STOP_CODE_RE = re.compile(r"\b\d{4,5}\b")

# GTFS-realtime ScheduleRelationship enum values we care about. The proto
# treats SCHEDULED as 0 and the explicit "no data" / "skipped" as 1/2/3, but
# we read by the descriptor so test fixtures don't have to match exactly.
_SCHEDULED = 0
_SKIPPED = 1
_NO_DATA = 2


# --- caches ------------------------------------------------------------------

# Cache the parsed StopTimeUpdate index, keyed on URL. The index shape is:
#   { stop_id: [ (trip_id, predicted_dt, delay_seconds, route_id, direction), ... ] }
# Recomputed when the feed is refetched. Per-stop bucketing means resolution
# scans only the relevant rows for each query.
_feed_index_cache: dict[str, tuple[float, dict[str, list]]] = {}

_qdrant_client: QdrantClient | None = None


def _get_qdrant_client() -> QdrantClient:
    """Lazy QdrantClient singleton — avoids opening a connection at import time."""
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    return _qdrant_client


# --- pydantic models ---------------------------------------------------------


class StopRef(BaseModel):
    stop_id: str
    stop_code: str
    stop_name: str
    stop_desc: str
    lat: float
    lon: float
    nextride_url: str
    display_label: str


class Arrival(BaseModel):
    route_id: str
    route_short_name: str
    route_long_name: str
    headsign: str
    direction_id: int | None = None
    predicted_time: datetime
    delay_seconds: int | None = None
    minutes_until: int
    stop_id: str
    stop_code: str
    stop_name: str
    stop_desc: str
    trip_id: str
    nextride_stop_url: str
    nextride_route_url: str


class ArrivalsResult(BaseModel):
    matched_stops: list[StopRef] = Field(default_factory=list)
    arrivals: list[Arrival] = Field(default_factory=list)
    has_realtime: bool = False
    resolution_method: str = "none"
    fetched_at: datetime
    error: str | None = None


# --- normalization + geometry helpers ----------------------------------------


def _normalize_for_match(s: str) -> str:
    """Lowercase, collapse whitespace, normalize 'and' -> '&'.

    Used to compare stop names against user query phrases. The 'and' -> '&'
    swap matters because RTD names use '&' but users type 'and'."""
    out = s.lower().strip()
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\band\b", "&", out)
    return out


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Earth radius rounded to 6371 km."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def _stop_code_to_stop_id() -> dict[str, str]:
    """Inverted view of stops_by_id keyed on stop_code for direct lookups."""
    return {
        stop.stop_code: stop.stop_id
        for stop in _rtd_static.stops_by_id().values()
        if stop.stop_code
    }


# --- stop resolution cascade -------------------------------------------------


def _resolve_by_stop_code(query: str) -> list[str]:
    """Return [stop_id] if the query mentions a known stop_code, else []."""
    code_map = _stop_code_to_stop_id()
    if not code_map:
        return []
    for token in STOP_CODE_RE.findall(query):
        sid = code_map.get(token)
        if sid:
            return [sid]
    return []


def _resolve_by_stop_name(query: str) -> list[str]:
    """Return all stop_ids whose stop_name appears as a substring of the query.

    Picks the longest matching stop_name to prefer specific over generic
    matches (e.g. 'Union Station Track 1' over 'Union Station'). Returns
    every stop_id sharing that exact name (paired directions)."""
    pairs = _rtd_static.stop_ids_by_name()
    if not pairs:
        return []
    qn = _normalize_for_match(query)
    best_name: str | None = None
    best_len = 0
    for name in pairs:
        if not name:
            continue
        nn = _normalize_for_match(name)
        if len(nn) <= best_len:
            continue
        if nn in qn:
            best_name = name
            best_len = len(nn)
    if best_name is None:
        return []
    return list(pairs[best_name])


def _get_neighborhood_centroid(neighborhood_name: str) -> tuple[float, float] | None:
    """Pull (lat, lon) from Qdrant's neighborhood_demographics docs.
    Returns None if the neighborhood is missing or has no centroid."""
    client = _get_qdrant_client()
    flt = Filter(
        must=[
            FieldCondition(
                key="metadata.neighborhood_name",
                match=MatchValue(value=neighborhood_name),
            )
        ]
    )
    points, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=flt,
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    payload = points[0].payload or {}
    meta = payload.get("metadata", {}) or {}
    loc = meta.get("location")
    if not loc:
        return None
    try:
        return float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _nearest_stop(lat: float, lon: float) -> _rtd_static.StopRecord | None:
    """Linear-scan all stops for the closest one within NEAREST_STOP_MAX_KM."""
    best: _rtd_static.StopRecord | None = None
    best_d = float("inf")
    for stop in _rtd_static.stops_by_id().values():
        d = _haversine_km(lat, lon, stop.lat, stop.lon)
        if d < best_d and d <= NEAREST_STOP_MAX_KM:
            best = stop
            best_d = d
    return best


def _resolve_by_neighborhood(query: str) -> list[str]:
    """Resolve neighborhood -> centroid -> nearest stop -> paired siblings."""
    resolved: ResolvedNeighborhood = resolve(query)
    if not resolved.name:
        return []
    centroid = _get_neighborhood_centroid(resolved.name)
    if centroid is None:
        return []
    lat, lon = centroid
    nearest = _nearest_stop(lat, lon)
    if nearest is None:
        return []
    pairs = _rtd_static.stop_ids_by_name()
    sibling_ids = pairs.get(nearest.stop_name) or [nearest.stop_id]
    return list(sibling_ids)


def resolve_stop_ids(query: str) -> tuple[list[str], str]:
    """Run the resolution cascade. Returns (stop_ids, method_used)."""
    if not query or not query.strip():
        return [], "none"
    stop_ids = _resolve_by_stop_code(query)
    if stop_ids:
        return stop_ids, "stop_code"
    stop_ids = _resolve_by_stop_name(query)
    if stop_ids:
        return stop_ids, "stop_name"
    stop_ids = _resolve_by_neighborhood(query)
    if stop_ids:
        return stop_ids, "neighborhood_centroid"
    return [], "none"


# --- feed fetch + parse ------------------------------------------------------


def _build_feed_index(feed) -> dict[str, list]:
    """Walk a parsed FeedMessage and return a stop_id-keyed index of upcoming
    arrivals.

    Each list entry is a tuple: (trip_id, predicted_dt, delay_seconds,
    route_id_from_descriptor, direction_id_from_descriptor). We index by
    stop_id so resolution scans only the relevant bucket per query.

    Skips StopTimeUpdate entries with schedule_relationship SKIPPED or
    NO_DATA, and entries lacking both arrival.time and departure.time.
    """
    index: dict[str, list] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip = tu.trip
        trip_id = trip.trip_id
        route_id_desc = trip.route_id if trip.HasField("route_id") else ""
        direction_desc = trip.direction_id if trip.HasField("direction_id") else None

        for stu in tu.stop_time_update:
            stop_id = stu.stop_id
            if not stop_id:
                continue
            sched_rel = (
                stu.schedule_relationship
                if stu.HasField("schedule_relationship")
                else _SCHEDULED
            )
            if sched_rel in (_SKIPPED, _NO_DATA):
                continue

            predicted_ts = 0
            delay_seconds: int | None = None
            if stu.HasField("arrival"):
                if stu.arrival.HasField("time"):
                    predicted_ts = stu.arrival.time
                if stu.arrival.HasField("delay"):
                    delay_seconds = stu.arrival.delay
            if predicted_ts == 0 and stu.HasField("departure"):
                if stu.departure.HasField("time"):
                    predicted_ts = stu.departure.time
                if stu.departure.HasField("delay") and delay_seconds is None:
                    delay_seconds = stu.departure.delay

            if predicted_ts == 0:
                continue

            predicted_dt = datetime.fromtimestamp(predicted_ts, tz=timezone.utc).astimezone(
                DENVER_TZ
            )
            index.setdefault(stop_id, []).append(
                (trip_id, predicted_dt, delay_seconds, route_id_desc, direction_desc)
            )
    return index


async def _fetch_and_index_feed(
    url: str | None = None,
) -> tuple[dict[str, list] | None, str | None]:
    """Fetch + parse + index TripUpdate.pb with a 30s TTL cache.

    Returns (index, error). On failure: (None, error_string). Failures are
    not cached.
    """
    target_url = url or RTD_TRIPUPDATES_URL
    now = time.time()
    cached = _feed_index_cache.get(target_url)
    if cached is not None and now - cached[0] < ARRIVALS_CACHE_TTL_SECONDS:
        return cached[1], None

    headers = {"User-Agent": USER_AGENT, "Accept": "application/x-protobuf"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(target_url)
            resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
    except Exception as exc:
        logger.warning("RTD trip updates fetch failed: %s", exc)
        return None, f"{type(exc).__name__}: {exc}"

    index = _build_feed_index(feed)
    _feed_index_cache[target_url] = (now, index)
    return index, None


# --- arrival construction ----------------------------------------------------


def _build_arrival_for_entry(
    entry: tuple,
    stop: _rtd_static.StopRecord,
    now: datetime,
) -> Arrival | None:
    """Enrich one feed-index entry with static GTFS metadata. Returns None if
    we lack enough static metadata to produce a useful Arrival (e.g. unknown
    trip_id with no route_id descriptor)."""
    trip_id, predicted_dt, delay_seconds, route_id_desc, direction_desc = entry

    trip_record = _rtd_static.trips_by_id().get(trip_id)
    route_id = (trip_record.route_id if trip_record else "") or route_id_desc
    if not route_id:
        return None

    headsign = trip_record.headsign if trip_record else ""
    direction = (
        trip_record.direction_id
        if trip_record and trip_record.direction_id is not None
        else direction_desc
    )

    route_record = _rtd_static.routes_by_id().get(route_id)
    short_name = route_record.short_name if route_record else route_id
    long_name = route_record.long_name if route_record else ""

    # Prefer the static record's nextride_url — it correctly uses
    # route_short_name for the rail/named-bus routes whose internal
    # route_id (e.g. "103W") returns 404 on NextRide.
    nextride_route_url = (
        route_record.nextride_url
        if route_record is not None
        else _rtd_static.NEXTRIDE_ROUTE_URL_TEMPLATE.format(route_id=route_id)
    )

    minutes_until = max(0, int((predicted_dt - now).total_seconds() // 60))

    return Arrival(
        route_id=route_id,
        route_short_name=short_name,
        route_long_name=long_name,
        headsign=headsign,
        direction_id=direction,
        predicted_time=predicted_dt,
        delay_seconds=delay_seconds,
        minutes_until=minutes_until,
        stop_id=stop.stop_id,
        stop_code=stop.stop_code,
        stop_name=stop.stop_name,
        stop_desc=stop.stop_desc,
        trip_id=trip_id,
        nextride_stop_url=stop.nextride_url,
        nextride_route_url=nextride_route_url,
    )


def _build_stop_refs(stop_ids: list[str]) -> list[StopRef]:
    """Materialize StopRef Pydantic objects for resolved stop_ids."""
    out: list[StopRef] = []
    stops_map = _rtd_static.stops_by_id()
    for sid in stop_ids:
        rec = stops_map.get(sid)
        if rec is None:
            continue
        out.append(
            StopRef(
                stop_id=rec.stop_id,
                stop_code=rec.stop_code,
                stop_name=rec.stop_name,
                stop_desc=rec.stop_desc,
                lat=rec.lat,
                lon=rec.lon,
                nextride_url=rec.nextride_url,
                display_label=rec.display_label,
            )
        )
    return out


# --- public API --------------------------------------------------------------


async def get_arrivals_for_query(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    url: str | None = None,
) -> ArrivalsResult:
    """Resolve a stop from `query`, fetch live arrivals, return a Pydantic
    result. Errors are returned as data via the `error` field — the function
    never raises."""
    fetched_at = datetime.now(timezone.utc)
    stop_ids, method = resolve_stop_ids(query)
    if not stop_ids:
        return ArrivalsResult(
            matched_stops=[],
            arrivals=[],
            has_realtime=False,
            resolution_method=method,
            fetched_at=fetched_at,
            error=(
                "Could not resolve a stop from the query. Mention a stop code, "
                "an intersection (e.g. '38th & Blake'), or a Denver neighborhood."
            ),
        )

    matched_stops = _build_stop_refs(stop_ids)
    index, fetch_error = await _fetch_and_index_feed(url=url)
    if fetch_error is not None or index is None:
        return ArrivalsResult(
            matched_stops=matched_stops,
            arrivals=[],
            has_realtime=False,
            resolution_method=method,
            fetched_at=fetched_at,
            error=fetch_error or "Unknown fetch error",
        )

    now_local = datetime.now(DENVER_TZ)
    cutoff = now_local + timedelta(minutes=window_minutes)
    stops_map = _rtd_static.stops_by_id()
    arrivals: list[Arrival] = []
    for sid in stop_ids:
        stop_rec = stops_map.get(sid)
        if stop_rec is None:
            continue
        for entry in index.get(sid, []):
            predicted_dt = entry[1]
            if predicted_dt < now_local or predicted_dt > cutoff:
                continue
            arrival = _build_arrival_for_entry(entry, stop_rec, now_local)
            if arrival is not None:
                arrivals.append(arrival)

    arrivals.sort(key=lambda a: a.predicted_time)
    capped = arrivals[:max_results]

    return ArrivalsResult(
        matched_stops=matched_stops,
        arrivals=capped,
        has_realtime=True,
        resolution_method=method,
        fetched_at=fetched_at,
        error=None,
    )


def clear_caches() -> None:
    """Clear in-process caches. Useful for tests and after re-extracting
    the GTFS bundle."""
    _feed_index_cache.clear()
    _stop_code_to_stop_id.cache_clear()
    _rtd_static.clear_caches()
