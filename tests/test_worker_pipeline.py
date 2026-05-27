"""Unit tests for the worker's top-level ingestion pipeline.

Mocks every collaborator (GCS download, parser, chunker, embedder,
Qdrant writer) so we can drive the orchestrator with deterministic
inputs and verify the resulting PointStruct payload matches the schema
locked in ITERATION_V2.md § Parsing + chunking strategy.
"""

from unittest.mock import MagicMock

import pytest
from qdrant_client.http.models import SparseVector

from worker.pipeline import process as process_module
from worker.pipeline.chunker import ChildChunk, ParentChunk
from worker.pipeline.parser import ParsedPage
from worker.pipeline.process import (
    DEFAULT_CATEGORY,
    SOURCE_COLLECTION,
    IngestResult,
    process_pdf,
)
from worker.pipeline.qdrant_writer import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME


@pytest.fixture
def patched_pipeline(monkeypatch) -> dict:
    """Replace every collaborator with a MagicMock. Returns the mocks so
    tests can configure return values and assert on calls."""
    download = MagicMock(return_value=b"%PDF-fake-bytes")
    parse = MagicMock(return_value=[ParsedPage(page_number=1, text="hello")])
    chunk = MagicMock(
        return_value=(
            [ParentChunk(parent_index=0, text="parent text", start_page=1, end_page=2)],
            [
                ChildChunk(
                    child_index=0,
                    text="child text 0",
                    start_page=1,
                    end_page=1,
                    parent_index=0,
                ),
                ChildChunk(
                    child_index=1,
                    text="child text 1",
                    start_page=2,
                    end_page=2,
                    parent_index=0,
                ),
            ],
        )
    )
    embed_dense = MagicMock(return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    embed_sparse = MagicMock(
        return_value=[
            ([1, 5, 100], [0.5, 0.3, 0.1]),
            ([2, 7, 99], [0.4, 0.6, 0.2]),
        ]
    )
    ensure_collection = MagicMock()
    upsert = MagicMock()

    monkeypatch.setattr(process_module, "download_pdf", download)
    monkeypatch.setattr(process_module, "parse_pdf", parse)
    monkeypatch.setattr(process_module, "chunk_pages", chunk)
    monkeypatch.setattr(process_module, "embed_dense", embed_dense)
    monkeypatch.setattr(process_module, "embed_sparse", embed_sparse)
    monkeypatch.setattr(process_module, "ensure_kb_collection", ensure_collection)
    monkeypatch.setattr(process_module, "upsert_chunks", upsert)

    return {
        "download": download,
        "parse": parse,
        "chunk": chunk,
        "embed_dense": embed_dense,
        "embed_sparse": embed_sparse,
        "ensure_collection": ensure_collection,
        "upsert": upsert,
    }


def _metadata(**overrides) -> dict:
    base = {
        "document_id": "pdfs/ordinance/2026-05-26-test.pdf",
        "document_title": "Denver Code of Ordinances",
        "original_filename": "municode-denver-co.pdf",
        "category": "ordinance",
        "source_url": "https://library.municode.com/...",
        "uploaded_at": "2026-05-26T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_process_pdf_returns_counts(patched_pipeline) -> None:
    result = process_pdf(
        bucket="b",
        object_name="pdfs/ordinance/x.pdf",
        custom_metadata=_metadata(),
    )
    assert isinstance(result, IngestResult)
    assert result.document_id == _metadata()["document_id"]
    assert result.parents == 1
    assert result.children == 2


def test_process_pdf_calls_pipeline_in_order(patched_pipeline) -> None:
    process_pdf(
        bucket="b",
        object_name="pdfs/ordinance/x.pdf",
        custom_metadata=_metadata(),
    )
    patched_pipeline["download"].assert_called_once_with(
        "b", "pdfs/ordinance/x.pdf"
    )
    patched_pipeline["parse"].assert_called_once()
    patched_pipeline["chunk"].assert_called_once()
    patched_pipeline["embed_dense"].assert_called_once_with(
        ["child text 0", "child text 1"]
    )
    patched_pipeline["embed_sparse"].assert_called_once_with(
        ["child text 0", "child text 1"]
    )
    patched_pipeline["ensure_collection"].assert_called_once()
    patched_pipeline["upsert"].assert_called_once()


def test_process_pdf_payload_shape_matches_spec(patched_pipeline) -> None:
    process_pdf(
        bucket="b",
        object_name="pdfs/ordinance/x.pdf",
        custom_metadata=_metadata(),
    )
    points = patched_pipeline["upsert"].call_args.args[0]
    assert len(points) == 2

    p0, p1 = points

    # IDs deterministic, distinct
    assert p0.id != p1.id

    # Hybrid: each point carries both named dense + sparse vectors
    assert p0.vector[DENSE_VECTOR_NAME] == [0.1, 0.2, 0.3]
    assert p1.vector[DENSE_VECTOR_NAME] == [0.4, 0.5, 0.6]
    assert isinstance(p0.vector[SPARSE_VECTOR_NAME], SparseVector)
    assert p0.vector[SPARSE_VECTOR_NAME].indices == [1, 5, 100]
    assert p0.vector[SPARSE_VECTOR_NAME].values == [0.5, 0.3, 0.1]
    assert p1.vector[SPARSE_VECTOR_NAME].indices == [2, 7, 99]
    assert p1.vector[SPARSE_VECTOR_NAME].values == [0.4, 0.6, 0.2]

    # Doc-level fields present and identical on both children
    for p in (p0, p1):
        assert p.payload["document_id"] == _metadata()["document_id"]
        assert p.payload["document_title"] == "Denver Code of Ordinances"
        assert p.payload["original_filename"] == "municode-denver-co.pdf"
        assert p.payload["category"] == "ordinance"
        assert p.payload["source_url"] == "https://library.municode.com/..."
        assert p.payload["source_collection"] == SOURCE_COLLECTION
        assert p.payload["uploaded_at"] == "2026-05-26T00:00:00Z"
        # Parent fields denormalized
        assert p.payload["parent_index"] == 0
        assert p.payload["parent_text"] == "parent text"
        assert p.payload["parent_start_page"] == 1
        assert p.payload["parent_end_page"] == 2

    # Child-level differs
    assert p0.payload["child_index"] == 0
    assert p0.payload["child_text"] == "child text 0"
    assert p0.payload["child_start_page"] == 1
    assert p0.payload["child_end_page"] == 1

    assert p1.payload["child_index"] == 1
    assert p1.payload["child_text"] == "child text 1"
    assert p1.payload["child_start_page"] == 2
    assert p1.payload["child_end_page"] == 2


def test_process_pdf_defaults_when_metadata_missing(patched_pipeline) -> None:
    """Missing optional fields → sensible defaults, doc_id falls back to object path."""
    result = process_pdf(
        bucket="b",
        object_name="pdfs/whatever/no-metadata.pdf",
        custom_metadata={},
    )
    points = patched_pipeline["upsert"].call_args.args[0]
    assert result.document_id == "pdfs/whatever/no-metadata.pdf"
    for p in points:
        assert p.payload["document_id"] == "pdfs/whatever/no-metadata.pdf"
        assert p.payload["document_title"] == ""
        assert p.payload["original_filename"] == ""
        assert p.payload["category"] == DEFAULT_CATEGORY
        assert p.payload["source_url"] == ""
        assert p.payload["uploaded_at"] == ""


def test_process_pdf_empty_pdf_skips_upsert(patched_pipeline) -> None:
    """If chunking produces zero children, embed + upsert should be skipped."""
    patched_pipeline["chunk"].return_value = ([], [])
    result = process_pdf(
        bucket="b",
        object_name="pdfs/empty.pdf",
        custom_metadata=_metadata(),
    )
    assert result.parents == 0
    assert result.children == 0
    patched_pipeline["embed_dense"].assert_not_called()
    patched_pipeline["embed_sparse"].assert_not_called()
    patched_pipeline["ensure_collection"].assert_not_called()
    patched_pipeline["upsert"].assert_not_called()
