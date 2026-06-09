"""Shared GCS v4 signed-URL helper — READ/download side.

Hosts the signer for the public knowledge-base download endpoint. The admin
*upload* (PUT) signer lives in app/admin.py and is intentionally left alone
(it was hard-won on Cloud Run — see deployment-pdf-kb.md A.2/A.3). Both share
the same keyless IAM `signBlob` trick; the credential logic is duplicated here
deliberately to avoid touching the proven upload path. TODO: once this read
path is also battle-tested, migrate admin to import `_signing_kwargs` from here
so there's a single copy.
"""

import logging
from datetime import timedelta

import google.auth
from google.auth import credentials as google_credentials
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage

logger = logging.getLogger(__name__)

DOWNLOAD_URL_TTL_MINUTES = 15


def _signing_kwargs(client: storage.Client) -> dict:
    """Extra kwargs so `generate_signed_url` works on Cloud Run.

    Key-backed creds (local SA key) sign in-process → no extra kwargs. Compute
    creds (Cloud Run ADC) hold only a bearer token and can't sign locally → we
    route through the IAM signBlob API, which needs a cloud-platform-scoped
    token (the storage client's own token is devstorage-scoped and signBlob
    rejects it with ACCESS_TOKEN_SCOPE_INSUFFICIENT). Mirrors app/admin.py.
    """
    if isinstance(client._credentials, google_credentials.Signing):
        return {}
    signer, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    signer.refresh(google_auth_requests.Request())
    return {
        "service_account_email": signer.service_account_email,
        "access_token": signer.token,
    }


def generate_download_url(
    bucket_name: str,
    object_path: str,
    ttl_minutes: int = DOWNLOAD_URL_TTL_MINUTES,
) -> str:
    """Short-lived v4 signed GET URL for downloading an object.

    Read-only, no bound headers (unlike the upload signer, which binds the
    x-goog-meta-* metadata into the signature). Raises FileNotFoundError if the
    object doesn't exist so the caller can return a clean 404.
    """
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_path)
    if not blob.exists():
        raise FileNotFoundError(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=ttl_minutes),
        method="GET",
        **_signing_kwargs(client),
    )
