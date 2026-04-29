"""Smoke-test the live RTD alerts feed end-to-end.

Calls app.tools.rtd_alerts.fetch_active_alerts() directly (no agent / no Qdrant)
and prints a human-readable summary. Useful for verifying:
  - Network reachability of RTD_ALERTS_URL
  - Protobuf parse success
  - Active-period filter behavior

Run from the project root (note the `-m` form — needed for `from app...` imports):
  python -m scripts.test_rtd_alerts
"""

import asyncio

from app.tools.rtd_alerts import RTD_ALERTS_URL, fetch_active_alerts


def _truncate(text: str, n: int = 160) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


async def main() -> int:
    print(f"Fetching: {RTD_ALERTS_URL}\n")
    result = await fetch_active_alerts()

    if result.error:
        print(f"❌ ERROR: {result.error}")
        return 1

    print(f"Fetched at: {result.fetched_at.isoformat()}")
    print(f"Active alerts: {len(result.alerts)}\n")

    if not result.alerts:
        print("(RTD has no currently-active alerts — that's a valid 'all clear' state.)")
        return 0

    for i, alert in enumerate(result.alerts, start=1):
        print(f"--- Alert {i} ---")
        print(f"  id:         {alert.alert_id}")
        print(f"  header:     {_truncate(alert.header)}")
        print(f"  effect:     {alert.effect}")
        print(f"  cause:      {alert.cause}")
        print(f"  routes:     {alert.affected_routes}")
        print(f"  stops:      {alert.affected_stops[:5]}{'...' if len(alert.affected_stops) > 5 else ''}")
        print(f"  active:     {alert.active_start} → {alert.active_end}")
        if alert.url:
            print(f"  url:        {alert.url}")
        if alert.description:
            print(f"  description: {_truncate(alert.description, 240)}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
