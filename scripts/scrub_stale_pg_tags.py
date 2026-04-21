"""scrub_stale_pg_tags.py

Removes stale metadata left over from the pre-refactor Postgres path: the keys
`pg_table`, `topic`, and `is_current` that `tag_qdrant_demographics.py` (now
deleted) wrote onto ~11 catalog-level service entries.

Only points whose `pg_table` field is set are touched. Neighborhood demographics
documents use `topic` legitimately (values like "population", "housing") and
DO NOT have `pg_table`, so the filter excludes them — their `topic` metadata
is preserved.

The old tagging script likely wrote these at the top level of the payload
(sibling of `metadata`) because it used set_payload without key="metadata".
We defensively try both top-level and metadata-nested patterns to cover either
layout.

Run: python scripts/scrub_stale_pg_tags.py
"""

import logging
import os
import sys

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

STALE_KEYS_TOP_LEVEL = ["pg_table", "topic", "is_current"]
STALE_KEYS_NESTED = ["metadata.pg_table", "metadata.topic", "metadata.is_current"]


def _filter_has_key(key: str) -> Filter:
    """Qdrant filter: the given payload key exists (is not empty)."""
    return Filter(
        must_not=[IsEmptyCondition(is_empty=PayloadField(key=key))]
    )


def _report_affected(client: QdrantClient, flt: Filter, location: str) -> list[str]:
    """Scroll to enumerate affected points; return a list of service_names."""
    service_names: list[str] = []
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=flt,
            limit=100,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        for pt in points:
            payload = pt.payload or {}
            meta = payload.get("metadata", {}) or {}
            service_names.append(meta.get("service_name") or payload.get("service_name") or "?")
        if next_offset is None:
            break
    if service_names:
        logger.info("%s — %d points to scrub: %s", location, len(service_names), service_names)
    else:
        logger.info("%s — no points found", location)
    return service_names


def main() -> int:
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

    # Pass 1: stale keys at the top level
    top_flt = _filter_has_key("pg_table")
    top_affected = _report_affected(client, top_flt, "top-level pg_table")

    if top_affected:
        try:
            client.delete_payload(
                collection_name=COLLECTION_NAME,
                keys=STALE_KEYS_TOP_LEVEL,
                points=top_flt,
            )
            logger.info(
                "deleted top-level keys %s from %d points",
                STALE_KEYS_TOP_LEVEL, len(top_affected),
            )
        except Exception as exc:
            logger.warning("top-level delete_payload failed: %s", exc)

    # Pass 2: stale keys nested inside metadata
    nested_flt = _filter_has_key("metadata.pg_table")
    nested_affected = _report_affected(client, nested_flt, "metadata.pg_table (nested)")

    if nested_affected:
        try:
            client.delete_payload(
                collection_name=COLLECTION_NAME,
                keys=STALE_KEYS_NESTED,
                points=nested_flt,
            )
            logger.info(
                "deleted nested keys %s from %d points",
                STALE_KEYS_NESTED, len(nested_affected),
            )
        except Exception as exc:
            logger.warning("nested delete_payload failed: %s", exc)

    total = len(top_affected) + len(nested_affected)
    if total == 0:
        logger.info("nothing to scrub — collection is already clean")
    else:
        logger.info("done — scrubbed stale pg tags from %d points total", total)

    return 0


if __name__ == "__main__":
    sys.exit(main())
