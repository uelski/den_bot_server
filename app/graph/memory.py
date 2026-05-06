"""memory.py — LangGraph checkpointer setup + conversation history helpers.

Backed by Redis via `langgraph-checkpoint-redis`'s `AsyncRedisSaver`. The
saver is constructed once per process at FastAPI startup (lifespan hook),
not per-request — keeps connection overhead low and avoids re-running
`asetup()` on every call.

When `REDIS_URL` is unset, `build_checkpointer()` returns `None` and the
graph compiles without a checkpointer. The `/query` endpoint still works
in this fallback (single-turn, no persistence across requests) — useful
for tests, CI, or quick local runs without Docker.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "REDIS_URL"
MEMORY_HISTORY_TURNS_ENV = "MEMORY_HISTORY_TURNS"
MEMORY_TTL_MINUTES_ENV = "MEMORY_TTL_MINUTES"

DEFAULT_HISTORY_TURNS = 8        # ≈4 user + 4 assistant exchanges
DEFAULT_TTL_MINUTES = 60 * 24 * 30  # 30 days


def get_history_turns() -> int:
    """How many prior messages to include in the generator prompt. Read at
    call time so tests can monkeypatch the env var without re-importing."""
    try:
        return max(0, int(os.getenv(MEMORY_HISTORY_TURNS_ENV, DEFAULT_HISTORY_TURNS)))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_TURNS


def get_ttl_minutes() -> int:
    """How long checkpointed thread state lives in Redis before auto-expiry.
    0 disables TTL (state lives indefinitely)."""
    try:
        return max(0, int(os.getenv(MEMORY_TTL_MINUTES_ENV, DEFAULT_TTL_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_TTL_MINUTES


async def build_checkpointer() -> Optional[AsyncRedisSaver]:
    """Construct + initialize the AsyncRedisSaver from REDIS_URL.

    Returns None when REDIS_URL is unset — caller should compile the graph
    without a checkpointer in that case (single-turn fallback). Raises on
    Redis connection failure when the URL IS set, to fail fast at startup
    rather than silently downgrading to in-memory.
    """
    redis_url = os.getenv(REDIS_URL_ENV)
    if not redis_url:
        logger.info(
            "REDIS_URL not set — skipping checkpointer (single-turn mode). "
            "Set REDIS_URL to enable multi-turn conversation memory."
        )
        return None

    ttl_minutes = get_ttl_minutes()
    ttl_config = {"default_ttl": ttl_minutes} if ttl_minutes > 0 else None

    saver = AsyncRedisSaver(redis_url=redis_url, ttl=ttl_config)
    await saver.asetup()
    logger.info(
        "AsyncRedisSaver initialized at %s (TTL: %s min)",
        redis_url,
        ttl_minutes if ttl_minutes > 0 else "disabled",
    )
    return saver


def truncate_history(messages: list[BaseMessage], max_messages: int | None = None) -> list[BaseMessage]:
    """Slice the message list down to the last `max_messages` entries.

    The full history always lives in Redis — this only controls how much is
    sent into the generator prompt to keep token usage bounded. Default
    comes from MEMORY_HISTORY_TURNS env var (8 messages ≈ 4 turns).

    Empty list / None inputs return [] cleanly so callers can blindly pass
    `state.get("messages")`.
    """
    if not messages:
        return []
    cap = max_messages if max_messages is not None else get_history_turns()
    if cap <= 0:
        return []
    return messages[-cap:]
