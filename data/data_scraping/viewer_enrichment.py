import json
import asyncio
import logging
from pathlib import Path
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_PATH = BASE_DIR / "enriched_denver_catalog_cleaned.json"
OUTPUT_PATH = BASE_DIR / "enriched_denver_catalog_v2.json"
HUB_BASE = "https://opendata-geospatialdenver.hub.arcgis.com/datasets"
REQUEST_DELAY = 0.2  # seconds between requests


# --- Retry decorator for transient HTTP failures ---
@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
    wait=wait_exponential(multiplier=1, min=1, max=16),  # 1s, 2s, 4s, 8s, 16s
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def fetch_json(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=False,  # Don't reraise on hub page check — just return None
)
async def check_hub_url(client: httpx.AsyncClient, url: str) -> bool:
    resp = await client.get(url, timeout=10.0)
    return resp.status_code == 200


async def enrich_record(client: httpx.AsyncClient, item: dict, index: int, total: int) -> dict:
    service_name = item.get("service_name", "unknown")
    base_url = item.get("base_url")

    # Default — explicit None so upsert script can do simple null check
    item["service_item_id"] = None
    item["hub_url"] = None

    if not base_url:
        logger.warning(f"[{index}/{total}] No base_url for {service_name}, skipping")
        return item

    # Step 1: Hit FeatureServer?f=json to get serviceItemId
    try:
        data = await fetch_json(client, f"{base_url}?f=json")
        await asyncio.sleep(REQUEST_DELAY)

        service_item_id = data.get("serviceItemId")
        if not service_item_id:
            logger.info(f"[{index}/{total}] No serviceItemId for {service_name}")
            return item

        item["service_item_id"] = service_item_id

    except Exception as e:
        logger.error(f"[{index}/{total}] Failed to fetch FeatureServer JSON for {service_name}: {e}")
        return item

    # Step 2: Verify the Hub page actually resolves
    hub_url = f"{HUB_BASE}/{service_item_id}/about"
    try:
        resolved = await check_hub_url(client, hub_url)
        await asyncio.sleep(REQUEST_DELAY)

        if resolved:
            item["hub_url"] = hub_url
            logger.info(f"[{index}/{total}] ✅ {service_name} → {hub_url}")
        else:
            logger.info(f"[{index}/{total}] ⚠️  Hub page not found for {service_name} (id: {service_item_id})")

    except Exception as e:
        logger.error(f"[{index}/{total}] Hub check failed for {service_name}: {e}")

    return item


async def main():
    with open(INPUT_PATH, "r") as f:
        records = json.load(f)

    total = len(records)
    logger.info(f"Starting enrichment for {total} records...")

    enriched = []
    resolved_count = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, item in enumerate(records, start=1):
            result = await enrich_record(client, item, i, total)
            enriched.append(result)
            if result.get("hub_url"):
                resolved_count += 1

    with open(OUTPUT_PATH, "w") as f:
        json.dump(enriched, f, indent=2)

    logger.info(f"Done. {resolved_count}/{total} records have a Hub URL.")
    logger.info(f"Output written to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())