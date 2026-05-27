"""Tests for /admin/* endpoints (password validation + signed URL issuance).

google.cloud.storage.Client is mocked everywhere so tests don't need
real GCP credentials or network. The fixture asserts on the args we
pass to `generate_signed_url` — that's where the tamper-proofing
contract is established (custom metadata bound to the signature).
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.admin import _reset_rate_limiter, _slugify_filename
from app.main import app


TEST_ADMIN_PASSWORD = "test-admin-password-do-not-use-in-prod"
TEST_BUCKET = "test-upload-bucket"


@pytest.fixture(autouse=True)
def _env_and_rate_limiter(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("GCS_UPLOAD_BUCKET", TEST_BUCKET)
    _reset_rate_limiter()


@pytest.fixture(autouse=True)
def signing_blob(monkeypatch):
    """Replace google.cloud.storage.Client. Returns the mock blob so
    tests can assert on what got passed to generate_signed_url."""
    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/test-upload-bucket/"
        "pdfs/ordinance/2026-05-26T120000-test.pdf?X-Goog-Signature=FAKE"
    )
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    monkeypatch.setattr(
        "app.admin.storage.Client", MagicMock(return_value=mock_client)
    )
    return mock_blob


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _valid_body() -> dict:
    return {
        "category": "ordinance",
        "document_title": "Denver Code of Ordinances",
        "original_filename": "Municode Denver CO.pdf",
        "source_url": "https://library.municode.com/co/denver/codes/code_of_ordinances",
    }


# ---------------------------------------------------------------------------
# /admin/validate-password
# ---------------------------------------------------------------------------


def test_validate_password_ok(client: TestClient) -> None:
    response = client.post(
        "/admin/validate-password",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_validate_password_wrong_returns_401(client: TestClient) -> None:
    response = client.post(
        "/admin/validate-password",
        headers={"X-Admin-Password": "nope"},
    )
    assert response.status_code == 401


def test_validate_password_missing_header_returns_401(client: TestClient) -> None:
    response = client.post("/admin/validate-password")
    assert response.status_code == 401


def test_validate_password_unset_on_server_returns_503(
    client: TestClient, monkeypatch
) -> None:
    """If ADMIN_PASSWORD env is missing we refuse to silently accept
    anything — that would be the dangerous failure mode."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    response = client.post(
        "/admin/validate-password",
        headers={"X-Admin-Password": "anything"},
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /admin/pdf-upload-url — happy path + response shape
# ---------------------------------------------------------------------------


def test_upload_url_returns_expected_shape(client: TestClient) -> None:
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=_valid_body(),
    )
    assert response.status_code == 200
    data = response.json()

    assert data["signed_url"].startswith("https://storage.googleapis.com/")
    assert data["object_path"].startswith("pdfs/ordinance/")
    assert data["object_path"].endswith(".pdf")

    headers = data["required_headers"]
    assert headers["Content-Type"] == "application/pdf"
    # Metadata keys use snake_case end-to-end (admin sets → GCS preserves →
    # worker reads via the same key). Keeps Python-side dicts idiomatic
    # without a hyphen↔underscore translation layer in two places.
    assert headers["x-goog-meta-category"] == "ordinance"
    assert headers["x-goog-meta-document_title"] == "Denver Code of Ordinances"
    assert headers["x-goog-meta-original_filename"] == "Municode Denver CO.pdf"
    assert headers["x-goog-meta-source_url"].startswith("https://library.municode.com")
    assert headers["x-goog-meta-document_id"] == data["object_path"]
    assert "x-goog-meta-uploaded_at" in headers


def test_upload_url_bakes_metadata_into_signed_url(
    client: TestClient, signing_blob
) -> None:
    """The frontend trusts that the metadata in `required_headers` is
    actually bound into the signed URL. Verify the call to
    generate_signed_url includes those exact headers."""
    client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=_valid_body(),
    )
    signing_blob.generate_signed_url.assert_called_once()
    kwargs = signing_blob.generate_signed_url.call_args.kwargs
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "PUT"
    assert kwargs["content_type"] == "application/pdf"

    bound = kwargs["headers"]
    assert bound["x-goog-meta-category"] == "ordinance"
    assert bound["x-goog-meta-document_title"] == "Denver Code of Ordinances"
    assert bound["x-goog-meta-original_filename"] == "Municode Denver CO.pdf"
    assert "x-goog-meta-document_id" in bound
    assert "x-goog-meta-uploaded_at" in bound


def test_upload_url_path_encodes_category(client: TestClient) -> None:
    body = _valid_body()
    body["category"] = "budget"
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    assert response.json()["object_path"].startswith("pdfs/budget/")


def test_upload_url_path_slugifies_filename(client: TestClient) -> None:
    body = _valid_body()
    body["original_filename"] = "Denver Budget FY2026 (FINAL).pdf"
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    object_path = response.json()["object_path"]
    assert object_path.endswith("-denver-budget-fy2026-final.pdf")


# ---------------------------------------------------------------------------
# /admin/pdf-upload-url — auth, validation, rate limit
# ---------------------------------------------------------------------------


def test_upload_url_rejects_wrong_password(client: TestClient) -> None:
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": "wrong"},
        json=_valid_body(),
    )
    assert response.status_code == 401


def test_upload_url_rejects_missing_password(client: TestClient) -> None:
    response = client.post("/admin/pdf-upload-url", json=_valid_body())
    assert response.status_code == 401


def test_upload_url_rejects_invalid_category(client: TestClient) -> None:
    body = _valid_body()
    body["category"] = "not-in-allowlist"
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    assert response.status_code == 422


def test_upload_url_rejects_extra_fields(client: TestClient) -> None:
    """extra='forbid' on the pydantic model — drift surfaces as 422."""
    body = _valid_body()
    body["malicious_extra"] = "value"
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    assert response.status_code == 422


def test_upload_url_rejects_empty_strings(client: TestClient) -> None:
    body = _valid_body()
    body["document_title"] = ""
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    assert response.status_code == 422


def test_upload_url_rejects_invalid_source_url(client: TestClient) -> None:
    body = _valid_body()
    body["source_url"] = "not-a-url"
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=body,
    )
    assert response.status_code == 422


def test_upload_url_503_when_bucket_unset(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.delenv("GCS_UPLOAD_BUCKET", raising=False)
    response = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=_valid_body(),
    )
    assert response.status_code == 503


def test_upload_url_rate_limit_kicks_in(client: TestClient) -> None:
    """5 requests within the window succeed; 6th gets 429."""
    for _ in range(5):
        ok = client.post(
            "/admin/pdf-upload-url",
            headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
            json=_valid_body(),
        )
        assert ok.status_code == 200
    too_many = client.post(
        "/admin/pdf-upload-url",
        headers={"X-Admin-Password": TEST_ADMIN_PASSWORD},
        json=_valid_body(),
    )
    assert too_many.status_code == 429


# ---------------------------------------------------------------------------
# Slugifier helper
# ---------------------------------------------------------------------------


def test_slugify_plain_pdf_filename() -> None:
    assert _slugify_filename("budget.pdf") == "budget.pdf"


def test_slugify_strips_punctuation_and_spaces() -> None:
    assert (
        _slugify_filename("Denver Budget FY2026 (FINAL).pdf")
        == "denver-budget-fy2026-final.pdf"
    )


def test_slugify_normalizes_non_pdf_extension_to_pdf() -> None:
    """We only handle PDFs; the path always ends in .pdf regardless of
    what the user picked. Original filename is preserved separately."""
    assert _slugify_filename("contract.DOCX") == "contract.pdf"


def test_slugify_handles_missing_extension() -> None:
    assert _slugify_filename("name-with-no-extension") == "name-with-no-extension.pdf"


def test_slugify_handles_empty_input() -> None:
    assert _slugify_filename("") == "untitled.pdf"


def test_slugify_collapses_repeated_separators() -> None:
    assert _slugify_filename("a---b___c   d.pdf") == "a-b-c-d.pdf"
