"""weather.py — National Weather Service lookups by Denver neighborhood.

Resolves a user phrase to an official neighborhood (via the resolver), pulls the
neighborhood's centroid lat/lon from Qdrant metadata, then queries the NWS API:

    GET /points/{lat},{lon}      -> properties.forecast (a URL)
    GET <that forecast URL>      -> properties.periods (forecast periods)

NWS is free and unauthenticated but requires a descriptive User-Agent. Set
NWS_USER_AGENT in .env to override the default.

Returns a `WeatherForecast` Pydantic model. On failure the model has `error`
populated and `periods` empty — callers should branch on `error` rather than
catching exceptions.
"""

import logging
import os
from functools import lru_cache

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.neighborhoods.resolver import ResolvedNeighborhood, resolve

load_dotenv()
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

NWS_BASE = "https://api.weather.gov"
USER_AGENT = os.getenv(
    "NWS_USER_AGENT",
    "blue-cypher-denver-rag (sam.vburgh@gmail.com)",
)
DEFAULT_PERIODS = 14  # NWS returns up to ~14 (about 7 days at day/night granularity)
NWS_TIMEOUT = 15.0


class ForecastPeriod(BaseModel):
    name: str
    start_time: str
    end_time: str
    is_daytime: bool
    temperature: int
    temperature_unit: str
    wind_speed: str
    wind_direction: str
    short_forecast: str
    detailed_forecast: str


class WeatherForecast(BaseModel):
    neighborhood_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    confidence: str | None = None  # passed through from resolver
    periods: list[ForecastPeriod] = Field(default_factory=list)
    error: str | None = None


_qdrant_client: QdrantClient | None = None


def _get_qdrant_client() -> QdrantClient:
    """Lazy QdrantClient singleton — avoids opening a connection at import time
    so tests can patch the module without needing a live Qdrant instance."""
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    return _qdrant_client


@lru_cache(maxsize=128)
def _get_neighborhood_location(neighborhood_name: str) -> tuple[float, float] | None:
    """Return (lat, lon) for a neighborhood from Qdrant metadata, or None."""
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


async def _fetch_nws_periods(
    lat: float, lon: float, max_periods: int = DEFAULT_PERIODS
) -> list[ForecastPeriod]:
    """Two-hop NWS call: /points -> forecast URL -> periods list.

    Raises on HTTP errors or malformed responses; the caller wraps and
    converts to a WeatherForecast.error.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(headers=headers, timeout=NWS_TIMEOUT) as client:
        points_resp = await client.get(f"{NWS_BASE}/points/{lat},{lon}")
        points_resp.raise_for_status()
        forecast_url = (points_resp.json().get("properties") or {}).get("forecast")
        if not forecast_url:
            raise ValueError("NWS /points response missing properties.forecast URL")

        forecast_resp = await client.get(forecast_url)
        forecast_resp.raise_for_status()
        raw_periods = (
            (forecast_resp.json() or {}).get("properties") or {}
        ).get("periods") or []

    out: list[ForecastPeriod] = []
    for p in raw_periods[:max_periods]:
        try:
            out.append(
                ForecastPeriod(
                    name=p["name"],
                    start_time=p["startTime"],
                    end_time=p["endTime"],
                    is_daytime=p["isDaytime"],
                    temperature=p["temperature"],
                    temperature_unit=p["temperatureUnit"],
                    wind_speed=p.get("windSpeed", ""),
                    wind_direction=p.get("windDirection", ""),
                    short_forecast=p.get("shortForecast", ""),
                    detailed_forecast=p.get("detailedForecast", ""),
                )
            )
        except KeyError as exc:
            logger.warning("skipping NWS period missing key %s: %s", exc, p)
    return out


async def get_weather_for_neighborhood(
    neighborhood_name: str, max_periods: int = DEFAULT_PERIODS
) -> WeatherForecast:
    """Fetch a forecast for a known canonical neighborhood name.

    `neighborhood_name` must already be one of the official Denver neighborhoods
    (use `resolve()` first if the input came from a user). Returns a
    WeatherForecast — `error` populated when something failed.
    """
    location = _get_neighborhood_location(neighborhood_name)
    if location is None:
        return WeatherForecast(
            neighborhood_name=neighborhood_name,
            error=(
                f"No centroid location stored for neighborhood "
                f"{neighborhood_name!r}. Run scripts/compute_neighborhood_centroids.py."
            ),
        )
    lat, lon = location

    try:
        periods = await _fetch_nws_periods(lat, lon, max_periods=max_periods)
    except Exception as exc:
        logger.warning(
            "NWS fetch failed for %s (%s, %s): %s",
            neighborhood_name, lat, lon, exc,
        )
        return WeatherForecast(
            neighborhood_name=neighborhood_name,
            lat=lat,
            lon=lon,
            error=f"NWS API error: {exc}",
        )

    return WeatherForecast(
        neighborhood_name=neighborhood_name,
        lat=lat,
        lon=lon,
        periods=periods,
    )


async def get_weather_for_query(
    query: str, max_periods: int = DEFAULT_PERIODS
) -> WeatherForecast:
    """Resolve a neighborhood from a raw user query, then fetch the forecast."""
    resolved: ResolvedNeighborhood = resolve(query)
    if not resolved.name:
        return WeatherForecast(
            confidence=resolved.confidence,
            error=(
                f"Could not resolve a Denver neighborhood from the query. "
                f"Resolver said: {resolved.reasoning}"
            ),
        )
    forecast = await get_weather_for_neighborhood(resolved.name, max_periods=max_periods)
    forecast.confidence = resolved.confidence
    return forecast


def clear_caches() -> None:
    """Clear the lat/lon lookup cache. Useful for tests and after re-running
    `compute_neighborhood_centroids.py`."""
    _get_neighborhood_location.cache_clear()
