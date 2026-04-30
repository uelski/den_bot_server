"""repair_rtd_route_urls.py

One-shot fix: existing rtd_route Qdrant points were upserted with NextRide
URLs built from the GTFS internal `route_id` (e.g. "103W"), which 404s on
NextRide. The rider-facing URL slug is `route_short_name` ("W").

This script scrolls all rtd_route points, identifies the ~12 routes where
route_id != route_short_name (rail + named bus circulators), and patches
their `metadata.base_url` and `metadata.hub_url` in place. Idempotent —
points already pointing at the short_name slug are skipped.

Run:
  python scripts/repair_rtd_route_urls.py            # apply
  python scripts/repair_rtd_route_urls.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList

# Make the project root importable so this standalone script can pull from app.tools.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools._rtd_static import nextride_route_slug  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

ROUTE_DOC_TYPE = "rtd_route"
URL_PREFIX = "https://app.rtd-denver.com/nextride/route/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing to Qdrant.",
    )
    args = parser.parse_args()

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

    flt = Filter(
        must=[
            FieldCondition(
                key="metadata.doc_type",
                match=MatchValue(value=ROUTE_DOC_TYPE),
            )
        ]
    )

    candidates: list[tuple[str, str, str, str, str]] = []  # (point_id, route_id, short_name, old_url, new_url)
    next_offset = None
    scanned = 0
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=flt,
            limit=500,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            scanned += 1
            meta = (p.payload or {}).get("metadata") or {}
            route_id = meta.get("route_id") or ""
            short_name = meta.get("route_short_name") or ""
            base_url = meta.get("base_url") or ""

            if not route_id and not short_name:
                continue
            slug = nextride_route_slug(route_id, short_name)
            if not slug:
                continue
            expected_url = f"{URL_PREFIX}{slug}"
            if base_url == expected_url:
                continue  # already correct (idempotent re-runs land here)

            candidates.append((str(p.id), route_id, short_name, base_url, expected_url))
        if next_offset is None:
            break

    logger.info(
        "scanned %d rtd_route points; %d need URL repair",
        scanned, len(candidates),
    )
    for point_id, route_id, short_name, old_url, new_url in candidates:
        logger.info(
            "  %s  route_id=%s  short=%s\n      old: %s\n      new: %s",
            point_id, route_id, short_name, old_url, new_url,
        )

    if args.dry_run:
        logger.info("--dry-run set, no writes performed")
        return 0
    if not candidates:
        logger.info("nothing to do")
        return 0

    patched = 0
    for point_id, _route_id, _short_name, _old, new_url in candidates:
        try:
            result = client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"base_url": new_url, "hub_url": new_url},
                points=PointIdsList(points=[point_id]),
                key="metadata",
            )
        except Exception as exc:
            logger.warning("failed to patch %s — %s", point_id, exc)
            continue
        logger.info(
            "patched %s  →  %s  (status=%s)",
            point_id, new_url, getattr(result, "status", "ok"),
        )
        patched += 1

    logger.info("done — patched %d rtd_route points", patched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
