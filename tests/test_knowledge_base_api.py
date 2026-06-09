"""Tests for the public /knowledge-base/* endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import knowledge_base as kb_api
from app.main import app

client = TestClient(app)


class TestListDocumentsEndpoint:
    def test_returns_documents_list(self):
        fake = [{"document_id": "pdfs/budget/x.pdf", "document_title": "Budget",
                 "source_url": "https://denvergov.org/b.pdf", "num_chunks": 3,
                 "page_count": 12}]
        with patch.object(kb_api, "list_documents", return_value=fake):
            resp = client.get("/knowledge-base/documents")
        assert resp.status_code == 200
        assert resp.json() == {"documents": fake}

    def test_empty_before_any_ingest(self):
        with patch.object(kb_api, "list_documents", return_value=[]):
            resp = client.get("/knowledge-base/documents")
        assert resp.status_code == 200
        assert resp.json() == {"documents": []}


class TestDownloadEndpoint:
    @pytest.fixture(autouse=True)
    def _bucket_env(self, monkeypatch):
        monkeypatch.setenv("GCS_UPLOAD_BUCKET", "test-bucket")

    def test_success_returns_signed_url(self):
        signed = "https://storage.googleapis.com/test-bucket/pdfs/budget/x.pdf?X-Goog-Signature=FAKE"
        with patch.object(kb_api, "generate_download_url", return_value=signed) as gen:
            resp = client.get(
                "/knowledge-base/documents/download",
                params={"document_id": "pdfs/budget/x.pdf"},
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "document_id": "pdfs/budget/x.pdf",
            "download_url": signed,
        }
        gen.assert_called_once_with("test-bucket", "pdfs/budget/x.pdf")

    def test_rejects_path_outside_upload_prefix(self):
        # Guards against signing arbitrary bucket objects.
        with patch.object(kb_api, "generate_download_url") as gen:
            resp = client.get(
                "/knowledge-base/documents/download",
                params={"document_id": "../secrets/key.json"},
            )
        assert resp.status_code == 400
        gen.assert_not_called()

    def test_missing_document_returns_404(self):
        with patch.object(
            kb_api, "generate_download_url", side_effect=FileNotFoundError("gone")
        ):
            resp = client.get(
                "/knowledge-base/documents/download",
                params={"document_id": "pdfs/budget/missing.pdf"},
            )
        assert resp.status_code == 404

    def test_missing_document_id_param_is_422(self):
        resp = client.get("/knowledge-base/documents/download")
        assert resp.status_code == 422

    def test_bucket_not_configured_returns_503(self, monkeypatch):
        monkeypatch.delenv("GCS_UPLOAD_BUCKET", raising=False)
        resp = client.get(
            "/knowledge-base/documents/download",
            params={"document_id": "pdfs/budget/x.pdf"},
        )
        assert resp.status_code == 503
