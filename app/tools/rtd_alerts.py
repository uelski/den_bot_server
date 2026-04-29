"""rtd_alerts.py — RTD GTFS-realtime service alerts.

Fetches the Alerts.pb feed at query time, parses the protobuf, filters to
currently-active alerts, optionally narrows by routes/stops mentioned in the
user's query, caps the returned list, and returns a structured Pydantic model.
Mirrors app/tools/weather.py: in-memory TTL cache, error-as-data return
contract, no exceptions surfaced to the caller.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GTFS_DIR = BASE_DIR / "data" / "rtd_gtfs"

RTD_ALERTS_URL = os.getenv(
    "RTD_ALERTS_URL",
    "https://open-data.rtd-denver.com/files/gtfs-rt/rtd/Alerts.pb",
)
RTD_ALERTS_VIEW_URL = os.getenv(
    "RTD_ALERTS_VIEW_URL", "https://app.rtd-denver.com/alerts"
)
USER_AGENT = os.getenv(
    "RTD_USER_AGENT",
    "blue-cypher-denver-rag (sam.vburgh@gmail.com)",
)
HTTP_TIMEOUT = 15.0
ALERTS_CACHE_TTL_SECONDS = int(os.getenv("RTD_ALERTS_CACHE_TTL_SECONDS", "300"))  # 5 min default
MAX_ALERTS_TO_LLM = int(os.getenv("RTD_ALERTS_MAX_TO_LLM", "5"))
_alerts_cache: dict[str, tuple[float, "ServiceAlertsResult"]] = {}


# --- GTFS-realtime cause/effect enum -> human strings -------------------------

# https://gtfs.org/realtime/reference/#enum-cause
_CAUSE_LABELS: dict[int, str] = {
    1: "UNKNOWN_CAUSE",
    2: "OTHER_CAUSE",
    3: "TECHNICAL_PROBLEM",
    4: "STRIKE",
    5: "DEMONSTRATION",
    6: "ACCIDENT",
    7: "HOLIDAY",
    8: "WEATHER",
    9: "MAINTENANCE",
    10: "CONSTRUCTION",
    11: "POLICE_ACTIVITY",
    12: "MEDICAL_EMERGENCY",
}
# https://gtfs.org/realtime/reference/#enum-effect
_EFFECT_LABELS: dict[int, str] = {
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    8: "UNKNOWN_EFFECT",
    9: "STOP_MOVED",
}


class ServiceAlert(BaseModel):
    alert_id: str
    header: str
    description: str
    cause: str | None = None
    effect: str | None = None
    affected_routes: list[str] = Field(default_factory=list)
    affected_stops: list[str] = Field(default_factory=list)
    active_start: datetime | None = None
    active_end: datetime | None = None
    url: str | None = None


class ServiceAlertsResult(BaseModel):
    alerts: list[ServiceAlert] = Field(default_factory=list)
    total_active: int = 0          # all currently-active alerts (pre-filter)
    filtered_count: int = 0        # after query-based filter (== total_active if no filter)
    alerts_url: str = ""           # canonical link to the full RTD alerts page
    fetched_at: datetime
    error: str | None = None


# --- GTFS ID maps for query filtering ----------------------------------------


@lru_cache(maxsize=1)
def _load_gtfs_id_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Load (route_id -> route_short_name) and (stop_id -> stop_code) maps.

    Read once and cached for the process lifetime. Returns empty dicts if the
    GTFS files are missing — in that case query filtering is a no-op (all
    active alerts are returned).
    """
    routes_path = GTFS_DIR / "routes.txt"
    stops_path = GTFS_DIR / "stops.txt"
    if not (routes_path.exists() and stops_path.exists()):
        logger.warning(
            "GTFS files not found in %s — RTD alert query filtering disabled",
            GTFS_DIR,
        )
        return {}, {}

    route_id_to_short_name: dict[str, str] = {}
    with routes_path.open(newline="") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            short = (row.get("route_short_name") or "").strip()
            if rid and short:
                route_id_to_short_name[rid] = short

    stop_id_to_stop_code: dict[str, str] = {}
    with stops_path.open(newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("stop_id") or "").strip()
            code = (row.get("stop_code") or "").strip()
            if sid and code:
                stop_id_to_stop_code[sid] = code

    logger.info(
        "loaded GTFS id maps: %d routes, %d stops",
        len(route_id_to_short_name), len(stop_id_to_stop_code),
    )
    return route_id_to_short_name, stop_id_to_stop_code


# --- protobuf -> Pydantic helpers --------------------------------------------


def _pick_translation(translated_string) -> str:
    """TranslatedString -> best available text (English preferred, else first)."""
    if not translated_string or not translated_string.translation:
        return ""
    for t in translated_string.translation:
        if t.language and t.language.lower().startswith("en"):
            return t.text
    return translated_string.translation[0].text


def _is_active(alert, now_ts: int) -> bool:
    """An Alert is active when any of its active_period TimeRanges contains now.

    Per the GTFS-realtime spec, an empty active_period list means "active until
    the alert is removed from the feed" — we treat that as active.
    """
    if not alert.active_period:
        return True
    for period in alert.active_period:
        start = period.start if period.HasField("start") else 0
        end = period.end if period.HasField("end") else 2**31 - 1
        if start <= now_ts <= end:
            return True
    return False


def _earliest_start(alert) -> datetime | None:
    starts = [p.start for p in alert.active_period if p.HasField("start")]
    if not starts:
        return None
    return datetime.fromtimestamp(min(starts), tz=timezone.utc)


def _latest_end(alert) -> datetime | None:
    ends = [p.end for p in alert.active_period if p.HasField("end")]
    if not ends:
        return None
    return datetime.fromtimestamp(max(ends), tz=timezone.utc)


def _to_service_alert(alert_id: str, alert) -> ServiceAlert:
    affected_routes: list[str] = []
    affected_stops: list[str] = []
    for selector in alert.informed_entity:
        if selector.route_id:
            affected_routes.append(selector.route_id)
        if selector.stop_id:
            affected_stops.append(selector.stop_id)

    cause = _CAUSE_LABELS.get(alert.cause) if alert.HasField("cause") else None
    effect = _EFFECT_LABELS.get(alert.effect) if alert.HasField("effect") else None

    return ServiceAlert(
        alert_id=alert_id,
        header=_pick_translation(alert.header_text),
        description=_pick_translation(alert.description_text),
        cause=cause,
        effect=effect,
        affected_routes=sorted(set(affected_routes)),
        affected_stops=sorted(set(affected_stops)),
        active_start=_earliest_start(alert),
        active_end=_latest_end(alert),
        url=_pick_translation(alert.url) or None,
    )


# --- query-based filtering ---------------------------------------------------


_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]+\b")


def _filter_alerts_by_query(
    alerts: list[ServiceAlert], query: str
) -> list[ServiceAlert]:
    """Return alerts whose affected routes (mapped to route_short_name) or
    stops (mapped to stop_code) appear as word tokens in the user's query.

    Rules:
      - Empty/whitespace query -> return all alerts unchanged.
      - GTFS id maps unavailable -> return all alerts unchanged.
      - Tokens are matched case-insensitively for routes ("W" matches "w line")
        and exactly for stop codes (numeric).
      - No alert matches -> return all alerts unchanged (graceful fallback,
        the LLM will summarize the broader set).
    """
    if not query or not query.strip():
        return alerts

    route_id_map, stop_id_map = _load_gtfs_id_maps()
    if not route_id_map and not stop_id_map:
        return alerts

    tokens = set(_TOKEN_RE.findall(query))
    tokens_upper = {t.upper() for t in tokens}

    matched: list[ServiceAlert] = []
    for alert in alerts:
        route_names = {
            (route_id_map.get(rid) or rid) for rid in alert.affected_routes
        }
        stop_codes = {
            (stop_id_map.get(sid) or sid) for sid in alert.affected_stops
        }

        route_hit = any(
            (rn or "").upper() in tokens_upper for rn in route_names if rn
        )
        stop_hit = any(sc in tokens for sc in stop_codes if sc)
        if route_hit or stop_hit:
            matched.append(alert)

    if not matched:
        return alerts
    return matched


# --- public API --------------------------------------------------------------


async def fetch_active_alerts(
    query: str = "", url: str | None = None
) -> ServiceAlertsResult:
    """Fetch RTD's Alerts.pb, parse, filter to currently-active alerts, narrow
    by routes/stops mentioned in `query`, and cap the returned list.

    Caching: results are cached per (url, query) pair for ALERTS_CACHE_TTL_SECONDS.
    Different query strings produce different cache entries because filtering
    happens on the parsed feed; the underlying HTTP fetch could be reused with
    a tighter design, but for now the simpler key wins (cost is one extra HTTP
    request per distinct query inside the TTL window).

    Failures are not cached — they return an empty result with `error` set and
    the next call retries.
    """
    target_url = url or RTD_ALERTS_URL
    cache_key = (target_url, query.strip().lower())
    now = time.time()

    cached = _alerts_cache.get(cache_key)
    if cached is not None and now - cached[0] < ALERTS_CACHE_TTL_SECONDS:
        return cached[1]

    headers = {"User-Agent": USER_AGENT, "Accept": "application/x-protobuf"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(target_url)
            resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
    except Exception as exc:
        logger.warning("RTD alerts fetch failed: %s", exc)
        return ServiceAlertsResult(
            alerts=[],
            total_active=0,
            filtered_count=0,
            alerts_url=RTD_ALERTS_VIEW_URL,
            fetched_at=datetime.now(timezone.utc),
            error=f"{type(exc).__name__}: {exc}",
        )

    now_ts = int(now)
    active_alerts: list[ServiceAlert] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        if not _is_active(entity.alert, now_ts):
            continue
        active_alerts.append(_to_service_alert(entity.id, entity.alert))

    total_active = len(active_alerts)
    filtered_alerts = _filter_alerts_by_query(active_alerts, query)
    filtered_count = len(filtered_alerts)
    capped_alerts = filtered_alerts[:MAX_ALERTS_TO_LLM]

    result = ServiceAlertsResult(
        alerts=capped_alerts,
        total_active=total_active,
        filtered_count=filtered_count,
        alerts_url=RTD_ALERTS_VIEW_URL,
        fetched_at=datetime.now(timezone.utc),
    )
    _alerts_cache[cache_key] = (now, result)
    return result


def clear_caches() -> None:
    """Clear the alerts cache and any cached GTFS id maps. Useful for tests."""
    _alerts_cache.clear()
    _load_gtfs_id_maps.cache_clear()
