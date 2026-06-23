"""Keepalive pings for managed free-tier backends (Qdrant Cloud + Redis Cloud).

Both free tiers reap resources after a period of inactivity. A scheduled
GET /ping (GCP Cloud Scheduler) touches each backend with a cheap
authenticated read so they register activity and don't get deleted.

Best-effort by design: each backend is touched independently, and a failure
of one is *reported*, not raised — so the keepalive still exercises the other
and the scheduler job doesn't flap on a transient blip. Real failures surface
in the response body + logs for monitoring.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

# Reuse the app's existing Qdrant client singleton (same URL/API key as the
# catalog + KB retrieval path) rather than opening a second connection.
from app.retrieval.kb import _get_client as _get_qdrant_client

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "REDIS_URL"


async def _touch_qdrant() -> None:
    """Cheap authenticated read against Qdrant. Raises on failure."""
    client = _get_qdrant_client()
    # get_collections() is blocking — run off the event loop.
    await asyncio.to_thread(client.get_collections)


async def _touch_redis() -> bool:
    """PING Redis. Returns True on success, False when REDIS_URL is unset
    (single-turn mode — there's no Redis resource to keep alive). Raises on a
    real connection failure."""
    redis_url = os.getenv(REDIS_URL_ENV)
    if not redis_url:
        return False
    # Imported lazily so the module loads even where redis isn't installed.
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    try:
        await client.ping()
        return True
    finally:
        await client.aclose()


async def ping_backends() -> dict:
    """Touch Qdrant + Redis to keep free-tier resources from being reaped.

    Never raises — returns a per-backend status dict so the scheduler job
    registers a success (it ran) while any real backend failure is visible in
    the response and logs.
    """
    result = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "qdrant": "skipped",
        "redis": "skipped",
    }

    try:
        await _touch_qdrant()
        result["qdrant"] = "ok"
    except Exception as exc:
        logger.warning("keepalive: qdrant touch failed: %s", exc)
        result["qdrant"] = "error"
        result["status"] = "degraded"

    try:
        redis_alive = await _touch_redis()
        result["redis"] = "ok" if redis_alive else "disabled"
    except Exception as exc:
        logger.warning("keepalive: redis touch failed: %s", exc)
        result["redis"] = "error"
        result["status"] = "degraded"

    return result
