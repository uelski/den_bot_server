from typing import Annotated, Any

from langchain_core.documents import Document
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # Conversation history — add_messages reducer appends rather than overwrites
    messages: Annotated[list, add_messages]

    # Current user query (may be rewritten on retry)
    query: str

    # Docs returned from Qdrant hybrid search
    retrieved_docs: list[Document]

    # Grader outputs
    docs_relevant: bool | None
    needs_scrape: bool  # Set by intent_router: True when has_layers=True AND query needs field/map detail

    # Retry tracking for query rewrite loop (max 2)
    retry_count: int

    # Scraper outputs — populated concurrently while generator streams
    scraped_layer_data: dict[str, Any] | None
    map_viewer_url: str | None  # emitted as a custom SSE event for the frontend
