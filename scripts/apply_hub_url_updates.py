"""apply_hub_url_updates.py

Reads data/hub_url_audit_report.json and patches the matching Qdrant points'
metadata in place (hub_url + service_item_id) using set_payload. No re-embedding,
no duplicates. Only catalog-level points are affected; neighborhood demographics
points are untouched because they do not carry the catalog's service_name.

Run: python scripts/apply_hub_url_updates.py
"""

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_PATH = BASE_DIR / "data" / "hub_url_audit_report.json"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")


def main() -> int:
    if not REPORT_PATH.exists():
        logger.error(
            "audit report not found at %s — run audit_hub_urls.py first",
            REPORT_PATH,
        )
        return 1

    with REPORT_PATH.open() as f:
        report = json.load(f)

    changes = [c for c in report.get("changes", []) if c["action"] in ("updated", "nulled")]
    if not changes:
        logger.info("no actionable changes in report (action in {updated, nulled}); nothing to do")
        return 0

    logger.info("found %d changes to apply to collection=%s", len(changes), COLLECTION_NAME)

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    updated_points = 0

    for change in changes:
        service_name = change["service_name"]
        new_hub_url = change["new_hub_url"]  # None for 'nulled'
        new_item_id = change["new_service_item_id"]

        flt = Filter(
            must=[
                FieldCondition(
                    key="metadata.service_name",
                    match=MatchValue(value=service_name),
                )
            ]
        )

        # Cleanup: remove any stale top-level hub_url / service_item_id
        # that earlier buggy runs of this script may have written as siblings
        # of `metadata` instead of inside it. Safe no-op when keys are absent.
        try:
            client.delete_payload(
                collection_name=COLLECTION_NAME,
                keys=["hub_url", "service_item_id"],
                points=flt,
            )
        except Exception as exc:
            logger.debug("cleanup delete_payload for %s: %s", service_name, exc)

        # Patch the nested metadata fields in place. `key="metadata"` nests the
        # payload dict under the metadata object where langchain-qdrant stores it.
        try:
            result = client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={
                    "hub_url": new_hub_url,
                    "service_item_id": new_item_id,
                },
                points=flt,
                key="metadata",
            )
        except Exception as exc:
            logger.warning("failed to patch %s — %s", service_name, exc)
            continue

        logger.info(
            "patched %s  →  metadata.hub_url=%s  metadata.service_item_id=%s  (status=%s)",
            service_name, new_hub_url, new_item_id, getattr(result, "status", "ok"),
        )
        updated_points += 1

    logger.info("done — patched %d catalog entries in Qdrant", updated_points)
    return 0


if __name__ == "__main__":
    sys.exit(main())
