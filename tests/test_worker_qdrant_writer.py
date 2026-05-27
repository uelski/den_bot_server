"""Unit tests for the Qdrant KB writer helpers.

Mocks the Qdrant client so we never touch a live server. The two
properties worth pinning down at the unit level:
  - `deterministic_point_id` is stable for a (document_id, child_index)
    pair (idempotency depends on this).
  - `ensure_kb_collection` short-circuits when the collection already
    exists and creates it otherwise.
"""

from unittest.mock import MagicMock

import pytest
from qdrant_client.http.models import Distance, SparseVectorParams, VectorParams

from worker.pipeline import qdrant_writer
from worker.pipeline.qdrant_writer import (
    DENSE_VECTOR_NAME,
    EMBEDDING_DIM,
    SPARSE_VECTOR_NAME,
    deterministic_point_id,
    ensure_kb_collection,
)


def test_deterministic_point_id_is_stable() -> None:
    a = deterministic_point_id("pdfs/ordinance/test.pdf", 7)
    b = deterministic_point_id("pdfs/ordinance/test.pdf", 7)
    assert a == b


def test_deterministic_point_id_differs_by_child_index() -> None:
    a = deterministic_point_id("pdfs/ordinance/test.pdf", 7)
    b = deterministic_point_id("pdfs/ordinance/test.pdf", 8)
    assert a != b


def test_deterministic_point_id_differs_by_document_id() -> None:
    a = deterministic_point_id("pdfs/ordinance/test.pdf", 7)
    b = deterministic_point_id("pdfs/budget/test.pdf", 7)
    assert a != b


def test_ensure_collection_skips_when_exists(monkeypatch) -> None:
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    qdrant_writer.get_client.cache_clear()
    monkeypatch.setattr(qdrant_writer, "get_client", lambda: mock_client)

    ensure_kb_collection()

    mock_client.collection_exists.assert_called_once()
    mock_client.create_collection.assert_not_called()


def test_ensure_collection_creates_hybrid_when_missing(monkeypatch) -> None:
    """Hybrid collection: named dense vector + named sparse vector."""
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = False
    qdrant_writer.get_client.cache_clear()
    monkeypatch.setattr(qdrant_writer, "get_client", lambda: mock_client)

    ensure_kb_collection()

    mock_client.create_collection.assert_called_once()
    kwargs = mock_client.create_collection.call_args.kwargs
    assert kwargs["collection_name"] == qdrant_writer.QDRANT_KB_COLLECTION_NAME

    # Dense vector named, correct size + distance
    dense_config = kwargs["vectors_config"][DENSE_VECTOR_NAME]
    assert isinstance(dense_config, VectorParams)
    assert dense_config.size == EMBEDDING_DIM
    assert dense_config.distance == Distance.COSINE

    # Sparse vector configured
    sparse_config = kwargs["sparse_vectors_config"][SPARSE_VECTOR_NAME]
    assert isinstance(sparse_config, SparseVectorParams)
