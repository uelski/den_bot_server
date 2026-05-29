"""Unit tests for the parent/child chunker.

Uses small parent/child token targets via monkeypatch so we don't need
multi-page lorem-ipsum fixtures to trigger multi-chunk output. The real
constants (1500/300 tokens) are validated implicitly by the splitter
itself; what we want to verify here is structural correctness:

  - Parents are produced from joined pages
  - Children link back to their parent via parent_index
  - Page-range tracking maps a chunk's char span to the right page numbers
  - Empty input → empty output
"""

import pytest

from worker.pipeline.chunker import (
    PAGE_JOINER,
    ChildChunk,
    ParentChunk,
    _page_offsets,
    _pages_for_span,
    chunk_pages,
)
from worker.pipeline.parser import ParsedPage


def _shrink_chunk_sizes(monkeypatch) -> None:
    """Reduce chunk targets so short fixtures still produce multiple chunks."""
    monkeypatch.setattr("worker.pipeline.chunker.PARENT_CHUNK_TOKENS", 40)
    monkeypatch.setattr("worker.pipeline.chunker.PARENT_CHUNK_OVERLAP", 4)
    monkeypatch.setattr("worker.pipeline.chunker.CHILD_CHUNK_TOKENS", 12)
    monkeypatch.setattr("worker.pipeline.chunker.CHILD_CHUNK_OVERLAP", 2)


def test_chunk_empty_pages_returns_empty() -> None:
    parents, children = chunk_pages([])
    assert parents == []
    assert children == []


def test_page_offsets_tracks_simple_pages() -> None:
    pages = [
        ParsedPage(page_number=1, text="aaa"),
        ParsedPage(page_number=2, text="bb"),
        ParsedPage(page_number=3, text="cccc"),
    ]
    offsets = _page_offsets(pages)
    # page 1: chars 0..2 (3 chars), joiner at 3
    # page 2: chars 4..5 (2 chars), joiner at 6
    # page 3: chars 7..10 (4 chars)
    assert offsets == [(0, 2, 1), (4, 5, 2), (7, 10, 3)]


def test_page_offsets_handles_empty_pages() -> None:
    """Empty pages must still occupy a position so page numbers stay aligned."""
    pages = [
        ParsedPage(page_number=1, text="aaa"),
        ParsedPage(page_number=2, text=""),
        ParsedPage(page_number=3, text="cc"),
    ]
    offsets = _page_offsets(pages)
    assert offsets[0] == (0, 2, 1)
    assert offsets[1][2] == 2  # page 2 still reported
    assert offsets[2][2] == 3


def test_pages_for_span_finds_correct_range() -> None:
    pages = [
        ParsedPage(page_number=1, text="aaa"),
        ParsedPage(page_number=2, text="bb"),
        ParsedPage(page_number=3, text="cccc"),
    ]
    offsets = _page_offsets(pages)
    # span entirely on page 1
    assert _pages_for_span(0, 2, offsets) == (1, 1)
    # span starting page 1 ending page 3
    assert _pages_for_span(0, 10, offsets) == (1, 3)
    # span on page 2 only
    assert _pages_for_span(4, 5, offsets) == (2, 2)


def test_chunk_single_page_short_text(monkeypatch) -> None:
    _shrink_chunk_sizes(monkeypatch)
    pages = [
        ParsedPage(
            page_number=1,
            text="The first chapter of the municipal code defines key terms.",
        )
    ]
    parents, children = chunk_pages(pages)
    assert len(parents) >= 1
    assert len(children) >= 1
    for child in children:
        assert child.parent_index < len(parents)
        assert child.start_page == 1
        assert child.end_page == 1


def test_chunk_multi_page_tracks_page_ranges(monkeypatch) -> None:
    _shrink_chunk_sizes(monkeypatch)
    # Build pages with distinct text so we can reason about boundaries
    pages = [
        ParsedPage(page_number=1, text="Alpha alpha alpha alpha alpha alpha alpha."),
        ParsedPage(page_number=2, text="Beta beta beta beta beta beta beta beta."),
        ParsedPage(page_number=3, text="Gamma gamma gamma gamma gamma gamma gamma."),
    ]
    parents, children = chunk_pages(pages)

    # We expect every produced chunk's pages to be within [1, 3]
    for parent in parents:
        assert 1 <= parent.start_page <= parent.end_page <= 3
    for child in children:
        assert 1 <= child.start_page <= child.end_page <= 3
        # Child's page range should be contained within (or equal to)
        # its parent's range
        parent = parents[child.parent_index]
        assert parent.start_page <= child.start_page
        assert child.end_page <= parent.end_page


def test_chunk_global_child_index_is_monotonic(monkeypatch) -> None:
    _shrink_chunk_sizes(monkeypatch)
    pages = [
        ParsedPage(page_number=i, text=f"Page {i} " * 30)
        for i in range(1, 5)
    ]
    _, children = chunk_pages(pages)
    indices = [c.child_index for c in children]
    assert indices == sorted(indices)
    assert indices == list(range(len(children)))


def test_chunk_returns_correct_types(monkeypatch) -> None:
    _shrink_chunk_sizes(monkeypatch)
    pages = [ParsedPage(page_number=1, text="hello world " * 10)]
    parents, children = chunk_pages(pages)
    assert all(isinstance(p, ParentChunk) for p in parents)
    assert all(isinstance(c, ChildChunk) for c in children)
