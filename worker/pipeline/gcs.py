"""Download an uploaded PDF object from GCS."""

import logging

from google.cloud import storage

logger = logging.getLogger(__name__)


def download_pdf(bucket_name: str, object_name: str) -> bytes:
    """Download the object's bytes from GCS.

    Raises:
        FileNotFoundError: object does not exist.
        Any underlying google.cloud.exceptions on transport/auth errors —
        the handler treats those as transient (Pub/Sub retries).
    """
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_name)
    if not blob.exists():
        raise FileNotFoundError(f"gs://{bucket_name}/{object_name} does not exist")
    data = blob.download_as_bytes()
    logger.info(
        "downloaded gs://%s/%s (%d bytes)", bucket_name, object_name, len(data)
    )
    return data
