"""admin.py — Admin endpoints for the PDF Knowledge Base upload flow.

Two endpoints:
  - POST /admin/validate-password : lets the frontend gate the admin UI
    without inventing a session concept. Returns 200/401.
  - POST /admin/pdf-upload-url    : returns a short-lived (10 min) GCS
    v4 signed URL with the upload's metadata baked into required
    x-goog-meta-* headers. The frontend then PUTs the PDF bytes
    directly to GCS — file bytes never touch this API.

Auth model: password-on-every-request via the `X-Admin-Password` header,
constant-time compared against ADMIN_PASSWORD. No JWT / token / session
— single admin, the password is the session. If multi-admin ever
becomes real, the upgrade path is Google OAuth + email allowlist (NOT
JWT on a shared password). See ITERATION_V2.md § Admin auth.

Tamper-proofing on the signed URL: the metadata is bound to the URL at
signing time as x-goog-meta-* headers. If the frontend's PUT in call 2
omits any of those headers or sends a different value, GCS returns 403.
That's the mechanism preventing the client from rewriting `category` or
`document_title` between issuance and upload.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
import google.auth
from google.auth import credentials as google_credentials
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"
GCS_UPLOAD_BUCKET_ENV = "GCS_UPLOAD_BUCKET"

# v4 signed URL lifetime. Short enough that a leaked URL can't be reused
# the next day; long enough that a slow connection still completes.
SIGNED_URL_TTL_MINUTES = 10

# Server-side category allowlist — the frontend MUST pick one of these.
# `general` is the catch-all so the upload form never blocks on a missing
# category. Mirrors the list locked in ITERATION_V2.md § Upload metadata.
ValidCategory = Literal[
    "ordinance",
    "council",
    "budget",
    "finance",
    "transparency",
    "general",
]

# Rate-limit on /admin/* to discourage credential stuffing against the
# password header. Best-effort (in-process, per-replica); upgrade to
# Redis-backed if we ever scale past one replica.
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _clean_env(value: str | None) -> str | None:
    """Mirror feedback.py's quote-stripping so secrets pasted into GCP
    Secret Manager with surrounding quotes produce the same runtime
    string as a dotenv-loaded `.env` value."""
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    return value or None


def _get_configured_password() -> str | None:
    return _clean_env(os.getenv(ADMIN_PASSWORD_ENV))


def _check_admin_password(provided: str | None) -> None:
    """Constant-time compare against ADMIN_PASSWORD.

    - 503 if ADMIN_PASSWORD is unset (we refuse to silently accept any
      password — that's the dangerous failure mode).
    - 401 on mismatch or missing header.
    """
    expected = _get_configured_password()
    if expected is None:
        logger.error("admin endpoint hit but ADMIN_PASSWORD env not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin auth not configured",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin password",
        )


# ---------------------------------------------------------------------------
# Rate limiter (per-IP sliding window — same pattern as feedback.py)
# ---------------------------------------------------------------------------


_request_log: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit_check(client_ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    log = _request_log[client_ip]
    while log and log[0] < cutoff:
        log.popleft()
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    log.append(now)
    return True


def _reset_rate_limiter() -> None:
    """Test-only — clear the per-IP request log."""
    _request_log.clear()


# ---------------------------------------------------------------------------
# Signed URL plumbing
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_filename(filename: str) -> str:
    """Convert an arbitrary filename to a safe URL slug ending in `.pdf`.

    We always normalize to .pdf because this system only accepts PDFs;
    the literal original filename is preserved separately as the
    `original_filename` metadata field.
    """
    name = filename.lower().strip()
    base = name.rpartition(".")[0] or name
    base = _SLUG_RE.sub("-", base).strip("-")
    if not base:
        base = "untitled"
    return f"{base}.pdf"


def _build_object_path(category: str, original_filename: str) -> str:
    """`pdfs/{category}/{ISO8601-compact}-{slug}.pdf`.

    Category in the path is for routing/inspection ease. The timestamp
    gives natural sort order and de-duplicates concurrent uploads of the
    same filename without colliding.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    return f"pdfs/{category}/{timestamp}-{_slugify_filename(original_filename)}"


def _meta_headers(metadata: dict[str, str]) -> dict[str, str]:
    """Build the `x-goog-meta-*` header dict from our metadata."""
    return {f"x-goog-meta-{k}": v for k, v in metadata.items()}


def _generate_signed_url(
    bucket_name: str,
    object_path: str,
    custom_metadata: dict[str, str],
) -> str:
    """Generate a v4 signed URL for PUT, binding custom metadata into
    the signature. Mismatched headers on the PUT → 403 from GCS.

    Signing path depends on the ambient credentials:

    - **Key-backed creds** (a local service-account key file) implement the
      Signing interface → the storage library signs in-process.
    - **No private key** (Cloud Run / GCE: ADC yields compute_engine creds
      that hold only a bearer token) → fall back to the IAM `signBlob` API by
      passing `service_account_email` + `access_token`. Without this, the
      library raises "you need a private key to sign credentials" and the
      endpoint 500s. The signBlob path requires the runtime SA to hold
      `roles/iam.serviceAccountTokenCreator` on itself + the IAM Service
      Account Credentials API enabled — see deployment-pdf-kb.md Phase 0.1 + A.2.

    The token used for signBlob must carry the **cloud-platform** scope. The
    storage client narrows its own token to `devstorage`, which `signBlob`
    rejects with `ACCESS_TOKEN_SCOPE_INSUFFICIENT` — so we mint a separate
    cloud-platform-scoped token rather than reusing `client._credentials`.
    """
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_path)

    signing_kwargs: dict[str, str] = {}
    if not isinstance(client._credentials, google_credentials.Signing):
        # Credentials can't sign locally (Cloud Run). Sign via IAM signBlob
        # using a cloud-platform-scoped token (the storage client's token is
        # devstorage-scoped → ACCESS_TOKEN_SCOPE_INSUFFICIENT).
        signer, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        signer.refresh(google_auth_requests.Request())
        signing_kwargs = {
            "service_account_email": signer.service_account_email,
            "access_token": signer.token,
        }

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=SIGNED_URL_TTL_MINUTES),
        method="PUT",
        content_type="application/pdf",
        headers=_meta_headers(custom_metadata),
        **signing_kwargs,
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UploadUrlRequest(BaseModel):
    """Body for POST /admin/pdf-upload-url.

    `extra="forbid"` makes the schema the contract — any unknown field
    is a 422 so frontend/backend drift surfaces immediately.
    """

    model_config = ConfigDict(extra="forbid")

    category: ValidCategory
    document_title: str = Field(..., min_length=1, max_length=300)
    original_filename: str = Field(..., min_length=1, max_length=300)
    # Optional: some PDFs have no canonical source URL (e.g. a local file).
    # When omitted the x-goog-meta-source_url header is left off entirely;
    # the worker defaults it to "" (worker/pipeline/process.py). If present
    # it must still be a valid http(s) URL.
    source_url: HttpUrl | None = None

    @field_validator("source_url", mode="before")
    @classmethod
    def _blank_source_url_to_none(cls, v: object) -> object:
        """A blank form field submits as "" — treat empty/whitespace as
        "not provided" so it coerces to None instead of failing HttpUrl
        validation. A non-empty invalid URL still 422s."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


class UploadUrlResponse(BaseModel):
    """Response from POST /admin/pdf-upload-url. Frontend spreads
    `required_headers` straight into its PUT call — no logic needed."""

    signed_url: str
    object_path: str
    required_headers: dict[str, str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/validate-password")
async def validate_password(
    x_admin_password: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """Login check for the frontend. Stateless: returns 200 on correct
    password, 401 otherwise. The frontend uses this to decide whether
    to show the admin UI; no session is created."""
    _check_admin_password(x_admin_password)
    return {"ok": True}


@router.post("/pdf-upload-url", response_model=UploadUrlResponse)
async def pdf_upload_url(
    body: UploadUrlRequest,
    request: Request,
    x_admin_password: Annotated[str | None, Header()] = None,
) -> UploadUrlResponse:
    """Issue a short-lived v4 signed URL for direct browser → GCS upload.

    Flow:
      1. Check password (X-Admin-Password header).
      2. Rate-limit per client IP.
      3. Validate body (category against allowlist, filename + title
         non-empty within length caps).
      4. Build the GCS object path: `pdfs/{category}/{ts}-{slug}.pdf`.
      5. Bake all six metadata fields into the signed URL as
         x-goog-meta-* headers. Frontend MUST echo them on the PUT.

    On success: 200 + {signed_url, object_path, required_headers}.
    The PDF bytes never touch this server; the Pub/Sub notification on
    the bucket fires when the PUT lands and delivers the metadata to
    the worker's /pubsub/pdf-ingest endpoint.
    """
    _check_admin_password(x_admin_password)

    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("admin pdf-upload-url rate limit hit from %s", client_ip)
        raise HTTPException(status_code=429, detail="too many requests")

    bucket_name = _clean_env(os.getenv(GCS_UPLOAD_BUCKET_ENV))
    if not bucket_name:
        logger.error("admin pdf-upload-url hit but GCS_UPLOAD_BUCKET env not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="upload bucket not configured",
        )

    object_path = _build_object_path(body.category, body.original_filename)
    uploaded_at = datetime.now(timezone.utc).isoformat()

    custom_metadata = {
        "document_id": object_path,
        "document_title": body.document_title,
        "original_filename": body.original_filename,
        "category": body.category,
        "uploaded_at": uploaded_at,
    }
    # Only bind source_url into the signature when provided — str(None) would
    # otherwise stamp the literal "None" into x-goog-meta-source_url.
    if body.source_url is not None:
        custom_metadata["source_url"] = str(body.source_url)

    try:
        signed_url = _generate_signed_url(bucket_name, object_path, custom_metadata)
    except Exception as exc:
        logger.exception("failed to generate signed url for %s", object_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to generate signed url: {exc}",
        )

    required_headers = {
        "Content-Type": "application/pdf",
        **_meta_headers(custom_metadata),
    }

    logger.info(
        "issued signed url: object_path=%s category=%s title=%r ip=%s",
        object_path,
        body.category,
        body.document_title,
        client_ip,
    )

    return UploadUrlResponse(
        signed_url=signed_url,
        object_path=object_path,
        required_headers=required_headers,
    )
