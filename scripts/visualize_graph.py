"""Render the LangGraph agent graph to a PNG file and optionally display it."""

from app.graph.orchestrator import graph

png_bytes = graph.get_graph().draw_mermaid_png()

output_path = "graph.png"
with open(output_path, "wb") as f:
    f.write(png_bytes)

print(f"Graph saved to {output_path}")
