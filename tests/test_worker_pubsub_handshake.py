"""Unit tests for the worker's Pub/Sub push handshake endpoint.

Hand-crafted Pub/Sub envelopes matching what Cloud Pub/Sub sends in
production push deliveries (with the GCS object.finalize event as the
base64-encoded `data` field). No emulator needed — the worker is just a
FastAPI endpoint that takes JSON, so we can drive it with TestClient.

These tests cover step A of the build sequence in ITERATION_V2.md: the
handshake itself (envelope parsed, structure validated, correct status
code returned). Step B will add the actual ingestion pipeline behind the
handler.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from worker.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _make_gcs_event(
    bucket: str = "test-pdf-bucket",
    object_name: str = "pdfs/ordinance/2026-05-26-test.pdf",
    custom_metadata: dict | None = None,
) -> dict:
    return {
        "kind": "storage#object",
        "bucket": bucket,
        "name": object_name,
        "contentType": "application/pdf",
        "size": "12345",
        "metadata": custom_metadata
        if custom_metadata is not None
        else {
            "document_title": "Denver Code of Ordinances",
            "source_url": "https://library.municode.com/...",
            "category": "ordinance",
            "original_filename": "municode-denver-co.pdf",
            "document_id": object_name,
            "uploaded_at": "2026-05-26T00:00:00Z",
        },
    }


def _make_envelope(gcs_event: dict | None = None, include_data: bool = True) -> dict:
    message: dict = {
        "messageId": "12345",
        "publishTime": "2026-05-26T00:00:00Z",
        "attributes": {
            "eventType": "OBJECT_FINALIZE",
        },
    }
    if include_data:
        event = gcs_event if gcs_event is not None else _make_gcs_event()
        message["data"] = base64.b64encode(json.dumps(event).encode()).decode()
    return {
        "subscription": "projects/test/subscriptions/pdf-ingest-sub",
        "message": message,
    }


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pdf_ingest_accepts_valid_envelope(client: TestClient) -> None:
    response = client.post("/pubsub/pdf-ingest", json=_make_envelope())
    assert response.status_code == 204


def test_pdf_ingest_accepts_envelope_with_empty_metadata(client: TestClient) -> None:
    """Custom metadata may be absent (e.g., a file written outside our
    signed-URL flow). Bucket + name are the only required fields."""
    event = _make_gcs_event(custom_metadata={})
    response = client.post("/pubsub/pdf-ingest", json=_make_envelope(event))
    assert response.status_code == 204


def test_pdf_ingest_rejects_missing_message(client: TestClient) -> None:
    response = client.post("/pubsub/pdf-ingest", json={})
    assert response.status_code == 400


def test_pdf_ingest_rejects_missing_data(client: TestClient) -> None:
    response = client.post(
        "/pubsub/pdf-ingest", json=_make_envelope(include_data=False)
    )
    assert response.status_code == 400


def test_pdf_ingest_rejects_invalid_base64(client: TestClient) -> None:
    bad_envelope = {
        "message": {
            "messageId": "12345",
            "data": "this is not base64 nor json!@#$",
        }
    }
    response = client.post("/pubsub/pdf-ingest", json=bad_envelope)
    assert response.status_code == 400


def test_pdf_ingest_rejects_data_that_is_not_json(client: TestClient) -> None:
    """Validly base64-encoded but not valid JSON after decode."""
    bad_envelope = {
        "message": {
            "messageId": "12345",
            "data": base64.b64encode(b"plain text not json").decode(),
        }
    }
    response = client.post("/pubsub/pdf-ingest", json=bad_envelope)
    assert response.status_code == 400


def test_pdf_ingest_rejects_missing_bucket(client: TestClient) -> None:
    event = {"name": "pdfs/ordinance/test.pdf", "metadata": {}}
    response = client.post("/pubsub/pdf-ingest", json=_make_envelope(event))
    assert response.status_code == 400


def test_pdf_ingest_rejects_missing_object_name(client: TestClient) -> None:
    event = {"bucket": "test-bucket", "metadata": {}}
    response = client.post("/pubsub/pdf-ingest", json=_make_envelope(event))
    assert response.status_code == 400
