"""Registry of tools the agent can call via bind_tools().

Each tool is a LangChain @tool-decorated function with a clear docstring (the
LLM reads docstrings as the tool's spec). To add a tool:
  1. Implement the underlying utility (e.g., app/tools/weather.py).
  2. Wrap with @tool here, returning a JSON-friendly dict.
  3. Append to AGENT_TOOLS.
"""

from langchain_core.tools import tool

from app.tools.rtd_alerts import ServiceAlertsResult, fetch_active_alerts
from app.tools.rtd_arrivals import (
    ArrivalsResult,
    DEFAULT_MAX_RESULTS,
    DEFAULT_WINDOW_MINUTES,
    get_arrivals_for_query,
)
from app.tools.rtd_vehicles import (
    DEFAULT_MAX_VEHICLES,
    VehiclePositionsResult,
    get_vehicle_positions_for_query,
)
from app.tools.weather import (
    DEFAULT_PERIODS,
    WeatherForecast,
    get_weather_for_query,
)


@tool
async def get_neighborhood_weather(query: str, max_periods: int = 4) -> dict:
    """Look up the weather forecast for a Denver neighborhood mentioned in the user's query.

    Args:
        query: The user's natural-language query mentioning a Denver neighborhood
            (e.g., "weather in RiNo this weekend", "is it cold in Capitol Hill").
            The neighborhood phrase can be slang, alias, or location cue.
        max_periods: How many forecast periods to return. NWS returns periods at
            day/night granularity, so 4 = next 2 days. Max 14 (~7 days). Default 4.

    Returns:
        A dict with the resolved neighborhood, lat/lon, and a list of forecast
        periods (each with name, start/end times, temperature, wind, short and
        detailed forecast text). On failure the dict has an `error` field set.
    """
    forecast: WeatherForecast = await get_weather_for_query(
        query, max_periods=min(max_periods, DEFAULT_PERIODS)
    )
    return forecast.model_dump()


@tool
async def get_rtd_service_alerts(query: str = "") -> dict:
    """Look up current RTD (Denver public transit) service alerts and disruptions.

    Use this for time-sensitive questions about whether RTD trains or buses are
    currently running, delayed, rerouted, single-tracking, or have any active
    service notices. Examples of when to call this tool:
      - "any service alerts on the W Line?"
      - "is RTD running on time today?"
      - "what's the status of Denver public transit right now?"

    Args:
        query: The user's full natural-language query. If it mentions a specific
            RTD route short name (e.g. "W", "15", "228A") or a stop code (e.g.
            "26134"), the tool filters returned alerts to those affecting that
            route or stop. If the query has no recognizable route/stop, all
            currently-active alerts are returned.

    Returns:
        A dict with:
          - `alerts`: capped list (default max 5) of relevant alert objects.
          - `total_active`: total count of currently-active alerts before filter.
          - `filtered_count`: count after query-based filter.
          - `alerts_url`: canonical URL the user can visit to see all alerts.
          - `fetched_at`: ISO timestamp.
          - `error`: set on fetch failure.
        An empty `alerts` list with `total_active: 0` means RTD has no active
        alerts — which is itself useful information.
    """
    result: ServiceAlertsResult = await fetch_active_alerts(query=query)
    return result.model_dump(mode="json")


@tool
async def get_rtd_next_arrivals(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict:
    """Look up live RTD train/bus arrival predictions at a Denver transit stop.

    Use this for time-sensitive "when's the next..." or "next arrivals at..."
    questions about a specific stop, intersection, or place. Examples:
      - "when's the next train at Union Station?"
      - "next bus at 38th & Blake"
      - "what trains are coming at stop 26134?"
      - "next arrivals near Capitol Hill"

    The tool resolves a stop in three ways: (1) a numeric stop code in the
    query, (2) a stop name like "Union Station" (returns both directions), or
    (3) a Denver neighborhood name (uses the neighborhood centroid + nearest
    stop). Predictions come from RTD's GTFS-realtime TripUpdate feed; the
    default window is the next 30 minutes.

    Args:
        query: The user's full natural-language query.
        max_results: Cap on returned arrivals (default 5).

    Returns:
        A dict with:
          - `matched_stops`: list of resolved stops, each with stop_id,
            stop_name, stop_desc, lat/lon, and a NextRide deep-link URL.
          - `arrivals`: list of predicted arrivals (route_short_name, headsign,
            predicted_time, minutes_until, delay_seconds, NextRide URLs, ...).
          - `has_realtime`: false on fetch failure or when no predictions found.
          - `resolution_method`: "stop_code" / "stop_name" /
            "neighborhood_centroid" / "none".
          - `fetched_at`: ISO timestamp.
          - `error`: set on fetch failure or when no stop could be resolved.
    """
    result: ArrivalsResult = await get_arrivals_for_query(
        query=query, max_results=max_results, window_minutes=DEFAULT_WINDOW_MINUTES
    )
    return result.model_dump(mode="json")


@tool
async def get_rtd_vehicle_positions(query: str, max_results: int = DEFAULT_MAX_VEHICLES) -> dict:
    """Look up the current location of RTD vehicles operating on a route.

    Use this for "where is..." or "where are..." questions about a specific
    train or bus line. Examples:
      - "where is the W Line right now?"
      - "where are the 15 buses?"
      - "is the A Line near Union Station?"
      - "where's the FreeRide?"

    The tool resolves the route via lexical match against route_short_name and
    route_long_name in RTD's static GTFS feed, then filters live
    VehiclePosition entries down to that route.

    Args:
        query: The user's full natural-language query.
        max_results: Cap on returned vehicles (default 25).

    Returns:
        A dict with:
          - `matched_route`: resolved route metadata (route_id, short_name,
            long_name, type_label, NextRide URL) or null if no match.
          - `vehicles`: list of vehicle snapshots (lat, lon, bearing, speed,
            current_status, headsign, reported_at, NextRide route URL, ...).
          - `has_realtime`: false on fetch failure or no positions on route.
          - `fetched_at`: ISO timestamp.
          - `error`: set on fetch failure or when no route could be resolved.
    """
    result: VehiclePositionsResult = await get_vehicle_positions_for_query(
        query=query, max_results=max_results
    )
    return result.model_dump(mode="json")


# Tools the tool_agent node binds to its LLM. Append here as new tools land.
AGENT_TOOLS = [
    get_neighborhood_weather,
    get_rtd_service_alerts,
    get_rtd_next_arrivals,
    get_rtd_vehicle_positions,
]
