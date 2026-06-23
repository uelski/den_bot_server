"""Tests for the /ping keepalive (touches Qdrant + Redis, never raises)."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app import keepalive
from app.main import app

client = TestClient(app)


class TestPingBackends:
    async def test_both_ok_when_redis_configured(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        qdrant = MagicMock()
        with patch.object(keepalive, "_get_qdrant_client", return_value=qdrant), \
            patch.object(keepalive, "_touch_redis", new=AsyncMock(return_value=True)):
            result = await keepalive.ping_backends()

        assert result["status"] == "ok"
        assert result["qdrant"] == "ok"
        assert result["redis"] == "ok"
        assert "timestamp" in result
        qdrant.get_collections.assert_called_once()

    async def test_redis_disabled_when_url_unset(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        with patch.object(keepalive, "_get_qdrant_client", return_value=MagicMock()):
            result = await keepalive.ping_backends()

        assert result["status"] == "ok"        # not an error — single-turn mode
        assert result["redis"] == "disabled"

    async def test_qdrant_failure_is_degraded_not_raised(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        boom = MagicMock()
        boom.get_collections.side_effect = RuntimeError("qdrant down")
        with patch.object(keepalive, "_get_qdrant_client", return_value=boom):
            result = await keepalive.ping_backends()

        assert result["status"] == "degraded"
        assert result["qdrant"] == "error"

    async def test_redis_failure_is_degraded_not_raised(self, monkeypatch):
        with patch.object(keepalive, "_get_qdrant_client", return_value=MagicMock()), \
            patch.object(
                keepalive, "_touch_redis",
                new=AsyncMock(side_effect=ConnectionError("redis down")),
            ):
            result = await keepalive.ping_backends()

        assert result["status"] == "degraded"
        assert result["redis"] == "error"


class TestPingRoute:
    def test_ping_endpoint_returns_status(self):
        canned = {"status": "ok", "timestamp": "2026-06-23T00:00:00+00:00",
                  "qdrant": "ok", "redis": "ok"}
        with patch("app.main.ping_backends", new=AsyncMock(return_value=canned)):
            resp = client.get("/ping")
        assert resp.status_code == 200
        assert resp.json() == canned
