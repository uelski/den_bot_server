"""Render the LangGraph agent graph to a PNG file and a mermaid source file.

PNG rendering uses the mermaid.ink remote API and can fail when that service is
unreachable. The mermaid source (.mmd) is always written as a reliable local
fallback — paste it into https://mermaid.live to render, or open in any
mermaid-aware tool.

Run: python -m scripts.visualize_graph
"""

from app.graph.orchestrator import graph

g = graph.get_graph()

# Always write the mermaid source — this never touches the network.
mmd_path = "graph.mmd"
with open(mmd_path, "w") as f:
    f.write(g.draw_mermaid())
print(f"Mermaid source saved to {mmd_path}")

# Try the PNG render with generous retries.
png_path = "graph.png"
try:
    png_bytes = g.draw_mermaid_png(max_retries=5, retry_delay=2.0)
    with open(png_path, "wb") as f:
        f.write(png_bytes)
    print(f"Graph PNG saved to {png_path}")
except Exception as exc:
    print(f"PNG render via mermaid.ink failed: {exc}")
    print(f"Fallback: open {mmd_path} in https://mermaid.live to view the graph.")
