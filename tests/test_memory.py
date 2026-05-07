"""Unit tests for app/graph/memory.

Covers the env-var-driven config helpers and the truncate_history slice
behavior. `build_checkpointer` is tested only at the env-var-fallback
boundary (no Redis connection required) — the actual AsyncRedisSaver
wiring is exercised in the manual smoke test described in the plan, not
in unit tests, because it requires a live Redis."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from app.graph import memory


class TestGetHistoryTurns:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(memory.MEMORY_HISTORY_TURNS_ENV, raising=False)
        assert memory.get_history_turns() == memory.DEFAULT_HISTORY_TURNS

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_HISTORY_TURNS_ENV, "12")
        assert memory.get_history_turns() == 12

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_HISTORY_TURNS_ENV, "not-a-number")
        assert memory.get_history_turns() == memory.DEFAULT_HISTORY_TURNS

    def test_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_HISTORY_TURNS_ENV, "-3")
        assert memory.get_history_turns() == 0


class TestGetTtlMinutes:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(memory.MEMORY_TTL_MINUTES_ENV, raising=False)
        assert memory.get_ttl_minutes() == memory.DEFAULT_TTL_MINUTES

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_TTL_MINUTES_ENV, "60")
        assert memory.get_ttl_minutes() == 60

    def test_zero_disables_ttl(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_TTL_MINUTES_ENV, "0")
        assert memory.get_ttl_minutes() == 0


class TestTruncateHistory:
    def _msgs(self, n: int) -> list:
        out = []
        for i in range(n):
            out.append(HumanMessage(content=f"q{i}"))
            out.append(AIMessage(content=f"a{i}"))
        return out

    def test_empty_input_returns_empty(self):
        assert memory.truncate_history([]) == []
        assert memory.truncate_history(None) == []  # type: ignore[arg-type]

    def test_returns_full_list_when_under_cap(self):
        msgs = self._msgs(2)  # 4 messages
        assert memory.truncate_history(msgs, max_messages=10) == msgs

    def test_slices_to_last_n_when_over_cap(self):
        msgs = self._msgs(10)  # 20 messages
        result = memory.truncate_history(msgs, max_messages=4)
        assert len(result) == 4
        # Should be the LAST 4, not the first 4.
        assert result[0].content == "q8"
        assert result[-1].content == "a9"

    def test_zero_cap_returns_empty(self):
        msgs = self._msgs(3)
        assert memory.truncate_history(msgs, max_messages=0) == []

    def test_uses_env_default_when_max_not_supplied(self, monkeypatch):
        monkeypatch.setenv(memory.MEMORY_HISTORY_TURNS_ENV, "2")
        msgs = self._msgs(5)  # 10 messages
        result = memory.truncate_history(msgs)
        assert len(result) == 2


class TestBuildCheckpointer:
    async def test_returns_none_when_redis_url_unset(self, monkeypatch):
        monkeypatch.delenv(memory.REDIS_URL_ENV, raising=False)
        result = await memory.build_checkpointer()
        assert result is None

    async def test_returns_none_when_redis_url_empty(self, monkeypatch):
        monkeypatch.setenv(memory.REDIS_URL_ENV, "")
        result = await memory.build_checkpointer()
        assert result is None
