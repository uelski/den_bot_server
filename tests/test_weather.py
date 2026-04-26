"""Unit tests for app.tools.weather."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.tools import weather as weather_module
from app.tools.weather import (
    ForecastPeriod,
    WeatherForecast,
    clear_caches,
    get_weather_for_neighborhood,
    get_weather_for_query,
)


# Sample fixture for a single NWS forecast period (matches the API shape)
SAMPLE_PERIOD = ForecastPeriod(
    name="Tonight",
    start_time="2026-04-26T18:00:00-06:00",
    end_time="2026-04-27T06:00:00-06:00",
    is_daytime=False,
    temperature=42,
    temperature_unit="F",
    wind_speed="5 to 10 mph",
    wind_direction="NW",
    short_forecast="Partly Cloudy",
    detailed_forecast="Partly cloudy, with a low around 42.",
)


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_caches()
    yield
    clear_caches()


def _patch_location(lat: float = 39.74, lon: float = -104.97):
    return patch.object(
        weather_module, "_get_neighborhood_location", return_value=(lat, lon)
    )


def _patch_no_location():
    return patch.object(
        weather_module, "_get_neighborhood_location", return_value=None
    )


def _patch_periods(periods=None):
    if periods is None:
        periods = [SAMPLE_PERIOD]
    return patch.object(
        weather_module, "_fetch_nws_periods", new=AsyncMock(return_value=periods)
    )


class TestGetWeatherForNeighborhoodHappyPath:
    @pytest.mark.asyncio
    async def test_returns_forecast_when_location_and_nws_succeed(self):
        with _patch_location(39.74, -104.97), _patch_periods([SAMPLE_PERIOD]):
            result = await get_weather_for_neighborhood("Capitol Hill")

        assert result.error is None
        assert result.neighborhood_name == "Capitol Hill"
        assert result.lat == 39.74
        assert result.lon == -104.97
        assert len(result.periods) == 1
        assert result.periods[0].temperature == 42

    @pytest.mark.asyncio
    async def test_passes_max_periods_through(self):
        many_periods = [SAMPLE_PERIOD] * 14
        with _patch_location(), patch.object(
            weather_module,
            "_fetch_nws_periods",
            new=AsyncMock(return_value=many_periods[:5]),
        ) as mock_fetch:
            result = await get_weather_for_neighborhood("Capitol Hill", max_periods=5)

        # We can't assert the exact slice (the mock controls that) but we can
        # assert max_periods was passed to the fetch function.
        mock_fetch.assert_called_once()
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs.get("max_periods") == 5
        assert len(result.periods) == 5


class TestGetWeatherForNeighborhoodFailures:
    @pytest.mark.asyncio
    async def test_missing_location_returns_error(self):
        with _patch_no_location():
            result = await get_weather_for_neighborhood("Capitol Hill")
        assert result.error is not None
        assert "No centroid location" in result.error
        assert result.periods == []
        assert result.lat is None and result.lon is None

    @pytest.mark.asyncio
    async def test_nws_exception_returns_error_with_lat_lon_preserved(self):
        with _patch_location(39.74, -104.97), patch.object(
            weather_module,
            "_fetch_nws_periods",
            new=AsyncMock(side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock(),
            )),
        ):
            result = await get_weather_for_neighborhood("Capitol Hill")

        assert result.error is not None
        assert "NWS API error" in result.error
        assert result.lat == 39.74
        assert result.lon == -104.97
        assert result.periods == []


class TestGetWeatherForQuery:
    @pytest.mark.asyncio
    async def test_resolves_then_fetches(self):
        from app.neighborhoods.resolver import ResolvedNeighborhood

        mock_resolved = ResolvedNeighborhood(
            name="Five Points", confidence="high", reasoning="alias"
        )
        with patch.object(weather_module, "resolve", return_value=mock_resolved), \
             _patch_location(39.76, -104.97), \
             _patch_periods([SAMPLE_PERIOD]):
            result = await get_weather_for_query("weather in RiNo")

        assert result.error is None
        assert result.neighborhood_name == "Five Points"
        assert result.confidence == "high"
        assert len(result.periods) == 1

    @pytest.mark.asyncio
    async def test_unresolved_query_returns_error_no_nws_call(self):
        from app.neighborhoods.resolver import ResolvedNeighborhood

        mock_resolved = ResolvedNeighborhood(
            name=None, confidence="low", reasoning="No neighborhood found."
        )
        with patch.object(weather_module, "resolve", return_value=mock_resolved), \
             patch.object(weather_module, "_fetch_nws_periods") as mock_fetch:
            result = await get_weather_for_query("what time is it?")

        assert result.error is not None
        assert "Could not resolve" in result.error
        assert result.confidence == "low"
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolver_confidence_passed_through_on_success(self):
        from app.neighborhoods.resolver import ResolvedNeighborhood

        mock_resolved = ResolvedNeighborhood(
            name="Five Points", confidence="medium", reasoning="ambiguous"
        )
        with patch.object(weather_module, "resolve", return_value=mock_resolved), \
             _patch_location(), _patch_periods():
            result = await get_weather_for_query("near Five Points or Globeville")

        assert result.confidence == "medium"
        assert result.neighborhood_name == "Five Points"


class TestFetchNwsPeriodsTwoHop:
    """Verify the two-hop NWS API logic with mocked httpx."""

    @pytest.mark.asyncio
    async def test_two_hop_request_chain(self):
        points_response = MagicMock(spec=httpx.Response)
        points_response.raise_for_status = MagicMock()
        points_response.json.return_value = {
            "properties": {"forecast": "https://api.weather.gov/gridpoints/BOU/64,68/forecast"}
        }

        forecast_response = MagicMock(spec=httpx.Response)
        forecast_response.raise_for_status = MagicMock()
        forecast_response.json.return_value = {
            "properties": {
                "periods": [
                    {
                        "name": "Tonight",
                        "startTime": "2026-04-26T18:00:00-06:00",
                        "endTime": "2026-04-27T06:00:00-06:00",
                        "isDaytime": False,
                        "temperature": 42,
                        "temperatureUnit": "F",
                        "windSpeed": "5 mph",
                        "windDirection": "NW",
                        "shortForecast": "Clear",
                        "detailedForecast": "Clear and cold.",
                    }
                ]
            }
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[points_response, forecast_response])
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            result = await weather_module._fetch_nws_periods(39.74, -104.97)

        assert mock_client.get.call_count == 2
        first_call_url = mock_client.get.call_args_list[0].args[0]
        assert "/points/39.74,-104.97" in first_call_url
        second_call_url = mock_client.get.call_args_list[1].args[0]
        assert "gridpoints" in second_call_url

        assert len(result) == 1
        assert result[0].name == "Tonight"
        assert result[0].temperature == 42

    @pytest.mark.asyncio
    async def test_missing_forecast_url_raises(self):
        points_response = MagicMock(spec=httpx.Response)
        points_response.raise_for_status = MagicMock()
        points_response.json.return_value = {"properties": {}}  # no forecast key

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=points_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="forecast URL"):
                await weather_module._fetch_nws_periods(39.74, -104.97)

    @pytest.mark.asyncio
    async def test_period_with_missing_required_field_is_skipped(self):
        points_response = MagicMock(spec=httpx.Response)
        points_response.raise_for_status = MagicMock()
        points_response.json.return_value = {
            "properties": {"forecast": "https://api.weather.gov/x"}
        }
        forecast_response = MagicMock(spec=httpx.Response)
        forecast_response.raise_for_status = MagicMock()
        forecast_response.json.return_value = {
            "properties": {
                "periods": [
                    # missing required 'temperature' key
                    {"name": "Bad period", "startTime": "x", "endTime": "y", "isDaytime": True},
                    {
                        "name": "Good period",
                        "startTime": "2026-04-26T18:00:00-06:00",
                        "endTime": "2026-04-27T06:00:00-06:00",
                        "isDaytime": True,
                        "temperature": 70,
                        "temperatureUnit": "F",
                    },
                ]
            }
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[points_response, forecast_response])
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            result = await weather_module._fetch_nws_periods(39.74, -104.97)

        # Only the well-formed period survives.
        assert len(result) == 1
        assert result[0].name == "Good period"


class TestGetNeighborhoodLocation:
    def test_returns_lat_lon_when_qdrant_has_location(self):
        mock_client = MagicMock()
        point = MagicMock()
        point.payload = {"metadata": {"location": {"lat": 39.74, "lon": -104.97}}}
        mock_client.scroll.return_value = ([point], None)

        with patch.object(weather_module, "_get_qdrant_client", return_value=mock_client):
            result = weather_module._get_neighborhood_location("Capitol Hill")

        assert result == (39.74, -104.97)

    def test_returns_none_when_no_points_match(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)

        with patch.object(weather_module, "_get_qdrant_client", return_value=mock_client):
            result = weather_module._get_neighborhood_location("Nowhere")

        assert result is None

    def test_returns_none_when_location_metadata_missing(self):
        mock_client = MagicMock()
        point = MagicMock()
        point.payload = {"metadata": {"neighborhood_name": "X"}}  # no location key
        mock_client.scroll.return_value = ([point], None)

        with patch.object(weather_module, "_get_qdrant_client", return_value=mock_client):
            result = weather_module._get_neighborhood_location("X")

        assert result is None
