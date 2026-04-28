"""Registry of tools the agent can call via bind_tools().

Each tool is a LangChain @tool-decorated function with a clear docstring (the
LLM reads docstrings as the tool's spec). To add a tool:
  1. Implement the underlying utility (e.g., app/tools/weather.py).
  2. Wrap with @tool here, returning a JSON-friendly dict.
  3. Append to AGENT_TOOLS.
"""

from langchain_core.tools import tool

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


# Tools the tool_agent node binds to its LLM. Append here as new tools land.
AGENT_TOOLS = [get_neighborhood_weather]
