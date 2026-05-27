"""Parent/child chunking with page tracking.

Concatenates parsed pages into a single text stream and records the
char-offset boundaries of each page so we can map every chunk back to
the page range it covers. Splits with `RecursiveCharacterTextSplitter`
twice — once into parents (~1500 tokens), then each parent into children
(~300 tokens). Children carry the `parent_index` link.

Page tracking uses `add_start_index=True` on the splitter so we get back
authoritative char offsets rather than re-running `str.find` (which can
get confused by repeated phrases or whitespace normalization). See
ITERATION_V2.md § Parsing + chunking strategy for the design rationale.
"""

import logging
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from worker.pipeline.parser import ParsedPage

logger = logging.getLogger(__name__)

PARENT_CHUNK_TOKENS = 1500
PARENT_CHUNK_OVERLAP = 150
CHILD_CHUNK_TOKENS = 300
CHILD_CHUNK_OVERLAP = 50

# Joiner between page texts. A single newline keeps the per-page offset
# arithmetic trivial (start_offset_of_next_page = end_offset_of_this + 1).
PAGE_JOINER = "\n"


@dataclass
class ParentChunk:
    parent_index: int
    text: str
    start_page: int
    end_page: int


@dataclass
class ChildChunk:
    child_index: int
    text: str
    start_page: int
    end_page: int
    parent_index: int


def _page_offsets(pages: list[ParsedPage]) -> list[tuple[int, int, int]]:
    """Return [(char_start, char_end_inclusive, page_number)] for each page
    in the joined text. char_end is inclusive — i.e., the last char index
    that still belongs to this page (NOT one-past-end)."""
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for page in pages:
        page_len = len(page.text)
        if page_len == 0:
            # Represent empty page as a zero-width span at the current cursor
            # so the page number is still discoverable by position.
            spans.append((cursor, cursor, page.page_number))
        else:
            spans.append((cursor, cursor + page_len - 1, page.page_number))
        cursor += page_len + len(PAGE_JOINER)
    return spans


def _pages_for_span(
    start: int, end_inclusive: int, offsets: list[tuple[int, int, int]]
) -> tuple[int, int]:
    """Find the first and last page numbers that intersect [start, end]."""
    start_page = offsets[0][2]
    end_page = offsets[-1][2]
    for off_start, off_end, page_num in offsets:
        if off_start <= start <= off_end:
            start_page = page_num
        if off_start <= end_inclusive <= off_end:
            end_page = page_num
            break
        # If end is past this page's range but we haven't hit a later one,
        # keep recording the highest page we've passed through.
        if end_inclusive > off_end:
            end_page = page_num
    return start_page, end_page


def chunk_pages(
    pages: list[ParsedPage],
) -> tuple[list[ParentChunk], list[ChildChunk]]:
    """Split parsed pages into parents and children with page tracking.

    Returns (parents, children). Children carry `parent_index` linking
    back to the parent they came from. `child_index` is global across the
    whole document, monotonically increasing — this is what gets fed to
    `deterministic_point_id` so the Qdrant IDs are stable.
    """
    if not pages:
        return [], []

    full_text = PAGE_JOINER.join(p.text for p in pages)
    offsets = _page_offsets(pages)

    parent_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=PARENT_CHUNK_TOKENS,
        chunk_overlap=PARENT_CHUNK_OVERLAP,
        add_start_index=True,
    )
    child_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=CHILD_CHUNK_TOKENS,
        chunk_overlap=CHILD_CHUNK_OVERLAP,
        add_start_index=True,
    )

    parent_docs = parent_splitter.create_documents([full_text])

    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    child_global_index = 0

    for parent_idx, parent_doc in enumerate(parent_docs):
        parent_start = parent_doc.metadata.get("start_index", 0)
        parent_text = parent_doc.page_content
        parent_end = parent_start + max(len(parent_text) - 1, 0)
        p_start_page, p_end_page = _pages_for_span(parent_start, parent_end, offsets)
        parents.append(
            ParentChunk(
                parent_index=parent_idx,
                text=parent_text,
                start_page=p_start_page,
                end_page=p_end_page,
            )
        )

        child_docs = child_splitter.create_documents([parent_text])
        for child_doc in child_docs:
            child_local_start = child_doc.metadata.get("start_index", 0)
            child_text = child_doc.page_content
            child_global_start = parent_start + child_local_start
            child_global_end = child_global_start + max(len(child_text) - 1, 0)
            c_start_page, c_end_page = _pages_for_span(
                child_global_start, child_global_end, offsets
            )
            children.append(
                ChildChunk(
                    child_index=child_global_index,
                    text=child_text,
                    start_page=c_start_page,
                    end_page=c_end_page,
                    parent_index=parent_idx,
                )
            )
            child_global_index += 1

    logger.info(
        "chunked: %d parents, %d children (parent~%d tok, child~%d tok)",
        len(parents),
        len(children),
        PARENT_CHUNK_TOKENS,
        CHILD_CHUNK_TOKENS,
    )
    return parents, children
