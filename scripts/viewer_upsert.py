import json
import logging
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_PATH = BASE_DIR / "data" / "enriched_denver_catalog_v2.json"
QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "denver_gis_catalog"


def upsert_hub_urls():
    client = QdrantClient(url=QDRANT_URL)

    with open(INPUT_PATH, "r") as f:
        records = json.load(f)

    total = len(records)
    updated = 0
    not_found = 0
    skipped = 0

    logger.info(f"Starting upsert for {total} records into '{COLLECTION_NAME}'...")

    for i, item in enumerate(records, start=1):
        service_name = item.get("service_name")
        hub_url = item.get("hub_url")           # May be None — we store it either way
        service_item_id = item.get("service_item_id")  # May be None

        # Find matching point in Qdrant by service_name
        try:
            results, _ = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="service_name",
                            match=MatchValue(value=service_name),
                        )
                    ]
                ),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
        except Exception as e:
            logger.error(f"[{i}/{total}] Scroll failed for {service_name}: {e}")
            skipped += 1
            continue

        if not results:
            logger.warning(f"[{i}/{total}] No Qdrant point found for {service_name}")
            not_found += 1
            continue

        point_id = results[0].id

        # Patch payload — sets both fields regardless of None so agent
        # can always do a simple null check without KeyError
        try:
            client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={
                    "hub_url": hub_url,
                    "service_item_id": service_item_id,
                },
                points=[point_id],
            )
            status = f"✅ {hub_url}" if hub_url else "⚠️  hub_url=None"
            logger.info(f"[{i}/{total}] {service_name} → {status}")
            updated += 1

        except Exception as e:
            logger.error(f"[{i}/{total}] set_payload failed for {service_name}: {e}")
            skipped += 1

    logger.info("--- Upsert complete ---")
    logger.info(f"  Updated:   {updated}")
    logger.info(f"  Not found: {not_found}")
    logger.info(f"  Skipped:   {skipped}")
    logger.info(f"  Total:     {total}")


if __name__ == "__main__":
    upsert_hub_urls()