"""audit_hub_urls.py

Audits each catalog entry's hub_url against the current ArcGIS item catalog.
For broken entries, attempts to rediscover the current item_id via URL-based
search. Writes a corrected catalog and a change report.

Inputs:  data/enriched_denver_catalog_v2.json
Outputs: data/enriched_denver_catalog_v3.json
         data/hub_url_audit_report.json

Run: python scripts/audit_hub_urls.py
"""

import json
import logging
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_PATH = BASE_DIR / "data" / "enriched_denver_catalog_v2.json"
OUTPUT_PATH = BASE_DIR / "data" / "enriched_denver_catalog_v3.json"
REPORT_PATH = BASE_DIR / "data" / "hub_url_audit_report.json"

HUB_BASE = "https://opendata-geospatialdenver.hub.arcgis.com/datasets"
ITEM_URL = "https://www.arcgis.com/sharing/rest/content/items/{id}"
SEARCH_URL = "https://www.arcgis.com/sharing/rest/search"
DENVER_GROUPS = [
    "36728b7c39914840a482030819d12011",
    "31a0c1babcc84c80b4ebcff5fecb159b",
    "e65d06e8620342a4ae6ef0ca91865036",
    "5cf40b91be174e8990a56cf000faf84e",
]

REQUEST_DELAY = 0.3


def item_exists(client: httpx.Client, item_id: str) -> bool:
    """Return True if the ArcGIS item with this ID still resolves."""
    try:
        resp = client.get(
            ITEM_URL.format(id=item_id),
            params={"f": "json"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("item check network error for %s — %s", item_id, exc)
        return False
    if resp.status_code != 200:
        return False
    data = resp.json()
    if "error" in data:
        return False
    return data.get("id") == item_id


def _url_variants(base_url: str) -> list[str]:
    """ArcGIS items store the FeatureServer URL with inconsistent casing in the
    /ArcGIS/ path segment (some records use /arcgis/). url:"..." search is
    case-sensitive, so try both variants."""
    variants = [base_url]
    lowered = base_url.replace("/ArcGIS/rest/", "/arcgis/rest/")
    if lowered != base_url:
        variants.append(lowered)
    else:
        uppered = base_url.replace("/arcgis/rest/", "/ArcGIS/rest/")
        if uppered != base_url:
            variants.append(uppered)
    return variants


def rediscover_by_url(client: httpx.Client, base_url: str):
    """Search for the current item backed by this FeatureServer URL, scoped to Denver groups.

    Returns (new_item_id, status, candidate_ids) where status is one of:
      'found', 'no_matches', 'ambiguous', 'search_failed'
    """
    group_query = " OR ".join(f'group:"{g}"' for g in DENVER_GROUPS)
    last_error = False

    for url_variant in _url_variants(base_url):
        q = f'url:"{url_variant}" AND (({group_query})) AND (-type:"Code Attachment")'
        try:
            resp = client.get(
                SEARCH_URL,
                params={"f": "json", "q": q, "num": 10},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("search network error for %s — %s", url_variant, exc)
            last_error = True
            continue
        if resp.status_code != 200:
            last_error = True
            continue
        results = resp.json().get("results", []) or []
        if not results:
            continue
        ids = [r.get("id") for r in results]
        if len(results) > 1:
            return None, "ambiguous", ids
        return results[0]["id"], "found", ids

    return (None, "search_failed", []) if last_error else (None, "no_matches", [])


def main() -> int:
    if not INPUT_PATH.exists():
        logger.error("input not found at %s", INPUT_PATH)
        return 1

    with INPUT_PATH.open() as f:
        catalog = json.load(f)

    logger.info("loaded %d catalog entries", len(catalog))

    changes: list[dict] = []
    stats = {
        "total": len(catalog),
        "with_hub_url": 0,
        "verified_live": 0,
        "updated": 0,
        "nulled_no_match": 0,
        "ambiguous_skipped": 0,
        "search_failed_skipped": 0,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    with httpx.Client(follow_redirects=True, headers=headers) as client:
        for idx, entry in enumerate(catalog, start=1):
            hub_url = entry.get("hub_url")
            if not hub_url:
                continue

            stats["with_hub_url"] += 1
            service_name = entry.get("service_name", "?")
            item_id = entry.get("service_item_id")
            base_url = entry.get("base_url")

            logger.info(
                "[%d/%d] checking %s (item_id=%s)",
                idx, len(catalog), service_name, item_id,
            )

            if item_id and item_exists(client, item_id):
                stats["verified_live"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            # Broken — try to rediscover by URL
            time.sleep(REQUEST_DELAY)
            new_id, status, candidates = rediscover_by_url(client, base_url) if base_url else (None, "search_failed", [])
            time.sleep(REQUEST_DELAY)

            if status == "found" and new_id:
                new_hub_url = f"{HUB_BASE}/{new_id}/about"
                logger.info(
                    "  → UPDATED  %s  %s → %s", service_name, item_id, new_id,
                )
                changes.append({
                    "service_name": service_name,
                    "base_url": base_url,
                    "old_service_item_id": item_id,
                    "old_hub_url": hub_url,
                    "new_service_item_id": new_id,
                    "new_hub_url": new_hub_url,
                    "action": "updated",
                })
                entry["service_item_id"] = new_id
                entry["hub_url"] = new_hub_url
                stats["updated"] += 1

            elif status == "no_matches":
                logger.info("  → NULLED   %s  (no Denver-group item backs %s)", service_name, base_url)
                changes.append({
                    "service_name": service_name,
                    "base_url": base_url,
                    "old_service_item_id": item_id,
                    "old_hub_url": hub_url,
                    "new_service_item_id": None,
                    "new_hub_url": None,
                    "action": "nulled",
                })
                entry["service_item_id"] = None
                entry["hub_url"] = None
                stats["nulled_no_match"] += 1

            elif status == "ambiguous":
                logger.warning(
                    "  → AMBIGUOUS %s  got %d candidates: %s (leaving as-is for manual review)",
                    service_name, len(candidates), candidates,
                )
                changes.append({
                    "service_name": service_name,
                    "base_url": base_url,
                    "old_service_item_id": item_id,
                    "old_hub_url": hub_url,
                    "action": "ambiguous",
                    "candidates": candidates,
                })
                stats["ambiguous_skipped"] += 1

            else:  # search_failed
                logger.warning("  → FAILED   %s  search failed (leaving as-is)", service_name)
                stats["search_failed_skipped"] += 1

    # Write corrected catalog
    with OUTPUT_PATH.open("w") as f:
        json.dump(catalog, f, indent=2)
    logger.info("wrote corrected catalog to %s", OUTPUT_PATH)

    # Write change report
    report = {"stats": stats, "changes": changes}
    with REPORT_PATH.open("w") as f:
        json.dump(report, f, indent=2)
    logger.info("wrote audit report to %s", REPORT_PATH)

    logger.info("summary: %s", json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
