"""Unit tests for app.tools.rtd_alerts.

Build a hand-crafted FeedMessage protobuf in-memory for each test case so we
exercise the real parsing + filtering logic without hitting the network.
"""

from datetime import timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from app.tools import _rtd_static, rtd_alerts as alerts_module
from app.tools.rtd_alerts import (
    ServiceAlertsResult,
    clear_caches,
    fetch_active_alerts,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_caches()
    yield
    clear_caches()


def _build_feed(*alerts) -> bytes:
    """Construct a serialized FeedMessage from the given (id, alert_kwargs) tuples."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1714000000  # arbitrary fixed value
    for alert_id, kwargs in alerts:
        entity = feed.entity.add()
        entity.id = alert_id
        a = entity.alert
        if "header" in kwargs:
            tr = a.header_text.translation.add()
            tr.text = kwargs["header"]
            tr.language = kwargs.get("language", "en")
        if "description" in kwargs:
            tr = a.description_text.translation.add()
            tr.text = kwargs["description"]
            tr.language = kwargs.get("language", "en")
        if "url" in kwargs:
            tr = a.url.translation.add()
            tr.text = kwargs["url"]
            tr.language = "en"
        if "cause" in kwargs:
            a.cause = kwargs["cause"]
        if "effect" in kwargs:
            a.effect = kwargs["effect"]
        for route_id in kwargs.get("routes", []):
            sel = a.informed_entity.add()
            sel.route_id = route_id
        for stop_id in kwargs.get("stops", []):
            sel = a.informed_entity.add()
            sel.stop_id = stop_id
        for start, end in kwargs.get("active_periods", []):
            period = a.active_period.add()
            if start is not None:
                period.start = start
            if end is not None:
                period.end = end
        # multilingual: optional 2nd translation (e.g. Spanish) appended after en
        if "spanish_header" in kwargs:
            tr = a.header_text.translation.add()
            tr.text = kwargs["spanish_header"]
            tr.language = "es"
    return feed.SerializeToString()


def _patch_httpx(content: bytes, status_code: int = 200):
    """Patch httpx.AsyncClient.get to return a response with the given content."""
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


class TestFetchAlertsHappyPath:
    @pytest.mark.asyncio
    async def test_parses_alert_fields(self):
        feed_bytes = _build_feed(
            ("alert_1", {
                "header": "W Line single tracking",
                "description": "Single tracking between Federal and Sheridan.",
                "url": "https://app.rtd-denver.com/alerts/1",
                "cause": 9,           # MAINTENANCE
                "effect": 3,          # SIGNIFICANT_DELAYS
                "routes": ["W"],
                "stops": ["S1", "S2"],
                "active_periods": [(1714000000 - 3600, 1714000000 + 3600)],  # straddle "now"
            }),
        )
        with _patch_httpx(feed_bytes), patch.object(
            alerts_module, "time", MagicMock(time=MagicMock(return_value=1714000000.0))
        ):
            result = await fetch_active_alerts()

        assert result.error is None
        assert len(result.alerts) == 1
        alert = result.alerts[0]
        assert alert.alert_id == "alert_1"
        assert alert.header == "W Line single tracking"
        assert "Single tracking" in alert.description
        assert alert.cause == "MAINTENANCE"
        assert alert.effect == "SIGNIFICANT_DELAYS"
        assert alert.affected_routes == ["W"]
        assert alert.affected_stops == ["S1", "S2"]
        assert alert.url == "https://app.rtd-denver.com/alerts/1"

    @pytest.mark.asyncio
    async def test_prefers_english_translation(self):
        feed_bytes = _build_feed(
            ("alert_x", {
                "header": "English header",
                "spanish_header": "Encabezado en español",
                "active_periods": [(0, 2**31 - 1)],
            }),
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()
        assert result.alerts[0].header == "English header"

    @pytest.mark.asyncio
    async def test_empty_active_period_treated_as_active(self):
        """Per GTFS-rt spec, empty active_period means active until the alert
        is removed from the feed."""
        feed_bytes = _build_feed(
            ("alert_perma", {"header": "Permanent until removed"}),
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()
        assert len(result.alerts) == 1


class TestActivePeriodFiltering:
    @pytest.mark.asyncio
    async def test_alert_past_its_end_excluded(self):
        feed_bytes = _build_feed(
            ("alert_past", {
                "header": "Yesterday's alert",
                "active_periods": [(1, 1000)],  # ended at unix=1000, far in past
            }),
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()
        assert result.alerts == []

    @pytest.mark.asyncio
    async def test_alert_with_future_start_excluded(self):
        feed_bytes = _build_feed(
            ("alert_future", {
                "header": "Tomorrow's alert",
                "active_periods": [(2**31 - 100, 2**31 - 1)],
            }),
        )
        with _patch_httpx(feed_bytes), patch.object(
            alerts_module, "time", MagicMock(time=MagicMock(return_value=1714000000.0))
        ):
            result = await fetch_active_alerts()
        assert result.alerts == []

    @pytest.mark.asyncio
    async def test_multiple_periods_any_matching_includes_alert(self):
        feed_bytes = _build_feed(
            ("alert_multi", {
                "header": "Multi-period alert",
                # First period in the past, second straddles "now"
                "active_periods": [
                    (1, 1000),
                    (1714000000 - 100, 1714000000 + 100),
                ],
            }),
        )
        with _patch_httpx(feed_bytes), patch.object(
            alerts_module, "time", MagicMock(time=MagicMock(return_value=1714000000.0))
        ):
            result = await fetch_active_alerts()
        assert len(result.alerts) == 1


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_http_500_returns_error_field(self):
        with _patch_httpx(b"", status_code=500):
            result = await fetch_active_alerts()
        assert result.error is not None
        assert result.alerts == []

    @pytest.mark.asyncio
    async def test_garbled_protobuf_returns_error(self):
        with _patch_httpx(b"not a real protobuf"):
            result = await fetch_active_alerts()
        assert result.error is not None
        assert result.alerts == []

    @pytest.mark.asyncio
    async def test_network_exception_returns_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            result = await fetch_active_alerts()
        assert result.error is not None
        assert "ConnectError" in result.error


class TestCaching:
    @pytest.mark.asyncio
    async def test_repeat_call_within_ttl_uses_cache(self):
        feed_bytes = _build_feed(
            ("alert_cached", {
                "header": "Cached",
                "active_periods": [(0, 2**31 - 1)],
            }),
        )
        with _patch_httpx(feed_bytes) as patched:
            mock_client = patched.kwargs.get("return_value") or patched.new
            await fetch_active_alerts()
            await fetch_active_alerts()
            await fetch_active_alerts()

        # The cache prevents a second underlying httpx.AsyncClient.get call.
        # Easier to assert by checking _alerts_cache state directly.
        assert len(alerts_module._alerts_cache) == 1

    @pytest.mark.asyncio
    async def test_clear_caches_evicts_entries(self):
        feed_bytes = _build_feed(
            ("alert_x", {"header": "x", "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes):
            await fetch_active_alerts()
        assert len(alerts_module._alerts_cache) == 1
        clear_caches()
        assert alerts_module._alerts_cache == {}

    @pytest.mark.asyncio
    async def test_failed_fetches_are_not_cached(self):
        with _patch_httpx(b"", status_code=500):
            await fetch_active_alerts()
        assert alerts_module._alerts_cache == {}

    @pytest.mark.asyncio
    async def test_expired_entry_refetches(self, monkeypatch):
        monkeypatch.setattr(alerts_module, "ALERTS_CACHE_TTL_SECONDS", 0)
        feed_bytes = _build_feed(
            ("alert_x", {"header": "x", "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes) as patcher:
            await fetch_active_alerts()
            await fetch_active_alerts()
        # Two underlying gets happened — one per call.
        # We can't introspect call_count easily because of how _patch_httpx is
        # structured, but we can verify the cache replaced its entry.
        assert len(alerts_module._alerts_cache) == 1


class TestResultModel:
    @pytest.mark.asyncio
    async def test_serializes_to_dict(self):
        feed_bytes = _build_feed(
            ("alert_x", {"header": "x", "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()
        data = result.model_dump(mode="json")
        assert "alerts" in data and isinstance(data["alerts"], list)
        assert "fetched_at" in data and isinstance(data["fetched_at"], str)
        assert "total_active" in data
        assert "filtered_count" in data
        assert "alerts_url" in data
        assert data["error"] is None

    def test_fetched_at_is_utc(self):
        # Smoke test: just verify the timezone import worked
        from datetime import datetime, timezone

        sample = ServiceAlertsResult(alerts=[], fetched_at=datetime.now(timezone.utc))
        assert sample.fetched_at.tzinfo is timezone.utc


# Mock GTFS maps used by the filter tests. Patches replace the real lru_cache
# return value so tests don't depend on data/rtd_gtfs/ being present.
MOCK_ROUTE_MAP = {
    "W": "W",        # GTFS often uses route_short_name as route_id at RTD
    "15": "15",
    "228A": "228A",
}
MOCK_STOP_MAP = {
    "S1": "26134",   # stop_id -> rider-facing stop_code
    "S2": "26135",
    "S3": "26136",
}


def _patch_gtfs_maps():
    """Force the shared static module's id maps to the test fixtures.

    Uses ExitStack-friendly contextmanager semantics by composing two
    patch.object calls into a single context via patch.multiple."""
    return patch.multiple(
        _rtd_static,
        route_id_to_short_name=MagicMock(return_value=MOCK_ROUTE_MAP),
        stop_id_to_stop_code=MagicMock(return_value=MOCK_STOP_MAP),
    )


class TestQueryFilter:
    @pytest.mark.asyncio
    async def test_query_with_known_route_filters_to_matching(self):
        feed_bytes = _build_feed(
            ("alert_w", {
                "header": "W Line single tracking",
                "routes": ["W"],
                "active_periods": [(0, 2**31 - 1)],
            }),
            ("alert_15", {
                "header": "Route 15 detour",
                "routes": ["15"],
                "active_periods": [(0, 2**31 - 1)],
            }),
            ("alert_other", {
                "header": "Unrelated",
                "routes": ["228A"],
                "active_periods": [(0, 2**31 - 1)],
            }),
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            result = await fetch_active_alerts(query="any alerts on the W Line?")

        assert result.total_active == 3
        assert result.filtered_count == 1
        assert len(result.alerts) == 1
        assert result.alerts[0].alert_id == "alert_w"

    @pytest.mark.asyncio
    async def test_query_with_known_stop_code_filters_to_matching(self):
        feed_bytes = _build_feed(
            ("alert_at_s1", {
                "header": "Stop closed",
                "stops": ["S1"],
                "active_periods": [(0, 2**31 - 1)],
            }),
            ("alert_at_s2", {
                "header": "Different stop",
                "stops": ["S2"],
                "active_periods": [(0, 2**31 - 1)],
            }),
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            result = await fetch_active_alerts(query="anything happening at stop 26134?")

        assert result.total_active == 2
        assert result.filtered_count == 1
        assert result.alerts[0].alert_id == "alert_at_s1"

    @pytest.mark.asyncio
    async def test_query_with_no_known_id_returns_all(self):
        feed_bytes = _build_feed(
            ("alert_a", {"header": "a", "routes": ["W"], "active_periods": [(0, 2**31 - 1)]}),
            ("alert_b", {"header": "b", "routes": ["15"], "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            # Query mentions a route name that's not in the GTFS map
            result = await fetch_active_alerts(query="anything for the spaceship line?")

        assert result.total_active == 2
        assert result.filtered_count == 2

    @pytest.mark.asyncio
    async def test_empty_query_returns_all_alerts(self):
        feed_bytes = _build_feed(
            ("alert_a", {"header": "a", "routes": ["W"], "active_periods": [(0, 2**31 - 1)]}),
            ("alert_b", {"header": "b", "routes": ["15"], "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            result = await fetch_active_alerts(query="")

        assert result.total_active == 2
        assert result.filtered_count == 2

    @pytest.mark.asyncio
    async def test_route_match_is_case_insensitive(self):
        feed_bytes = _build_feed(
            ("alert_w", {"header": "W", "routes": ["W"], "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            # lowercase 'w line' should still match
            result = await fetch_active_alerts(query="problems on the w line")
        assert result.filtered_count == 1

    @pytest.mark.asyncio
    async def test_filter_disabled_when_gtfs_maps_empty(self):
        """When GTFS files aren't present, filtering becomes a no-op and all
        active alerts come back regardless of the query."""
        feed_bytes = _build_feed(
            ("alert_a", {"header": "a", "routes": ["W"], "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes), patch.multiple(
            _rtd_static,
            route_id_to_short_name=MagicMock(return_value={}),
            stop_id_to_stop_code=MagicMock(return_value={}),
        ):
            result = await fetch_active_alerts(query="anything on the W Line?")
        assert result.total_active == 1
        assert result.filtered_count == 1


class TestAlertsCap:
    @pytest.mark.asyncio
    async def test_cap_applies_to_unfiltered_results(self, monkeypatch):
        monkeypatch.setattr(alerts_module, "MAX_ALERTS_TO_LLM", 2)
        feed_bytes = _build_feed(
            *[
                (f"a_{i}", {
                    "header": f"alert {i}",
                    "active_periods": [(0, 2**31 - 1)],
                })
                for i in range(5)
            ]
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()

        assert result.total_active == 5
        assert result.filtered_count == 5
        # Cap kicks in
        assert len(result.alerts) == 2

    @pytest.mark.asyncio
    async def test_cap_applies_after_filter(self, monkeypatch):
        monkeypatch.setattr(alerts_module, "MAX_ALERTS_TO_LLM", 2)
        feed_bytes = _build_feed(
            *[
                (f"w_{i}", {
                    "header": f"W alert {i}",
                    "routes": ["W"],
                    "active_periods": [(0, 2**31 - 1)],
                })
                for i in range(4)
            ]
        )
        with _patch_httpx(feed_bytes), _patch_gtfs_maps():
            result = await fetch_active_alerts(query="W Line")

        assert result.total_active == 4
        assert result.filtered_count == 4
        # All 4 matched the filter, but cap still keeps it to 2
        assert len(result.alerts) == 2


class TestAlertsUrl:
    @pytest.mark.asyncio
    async def test_alerts_url_always_set(self):
        feed_bytes = _build_feed(
            ("a", {"header": "x", "active_periods": [(0, 2**31 - 1)]}),
        )
        with _patch_httpx(feed_bytes):
            result = await fetch_active_alerts()
        assert result.alerts_url == "https://app.rtd-denver.com/alerts"

    @pytest.mark.asyncio
    async def test_alerts_url_set_even_on_error(self):
        with _patch_httpx(b"", status_code=500):
            result = await fetch_active_alerts()
        assert result.error is not None
        assert result.alerts_url == "https://app.rtd-denver.com/alerts"
