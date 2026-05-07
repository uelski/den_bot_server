from typing import Annotated, Any

from langchain_core.documents import Document
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # Conversation history — add_messages reducer appends rather than overwrites
    messages: Annotated[list, add_messages]

    # Current user query — kept stable across the turn so the generator
    # always answers the literal question the user asked.
    query: str

    # Throwaway, condensed/rewritten form of `query` used ONLY for Qdrant
    # retrieval. Set by the condenser (history-aware standalone-query
    # rewrite) and overwritten by the rewriter on retry. Never persisted
    # to messages — see CLAUDE-style note in app/graph/nodes/condenser.py.
    search_query: str | None

    # Gate: True when query needs RAG retrieval, False for general conversation
    requires_rag: bool

    # Docs returned from Qdrant hybrid search
    retrieved_docs: list[Document]

    # Grader outputs
    docs_relevant: bool | None
    needs_scrape: bool  # Set by intent_router: True when has_layers=True AND query needs field/map detail

    # Retry tracking for query rewrite loop (max 2)
    retry_count: int

    # Tool path (set by main_router; populated by tool_agent)
    needs_tool: bool
    tool_results: list[dict[str, Any]] | None  # list of {tool, args, result} per call

    # Scraper outputs — populated concurrently while generator streams
    scraped_layer_data: dict[str, Any] | None
    map_viewer_urls: list[str]  # hub page URLs + optional scraped viewer URL
