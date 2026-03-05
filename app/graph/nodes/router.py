"""main_router node — pass-through extensibility hook.

Runs at graph entry before retrieval. Currently a no-op but provides a named
node for future routing logic (e.g., multi-source dispatch, query classification
for different data backends).
"""

from app.graph.state import AgentState


def main_router(state: AgentState) -> dict:
    """Pass-through node. Returns empty dict — no state changes."""
    return {}
