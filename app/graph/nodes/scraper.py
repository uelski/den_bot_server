"""scraper node — async ArcGIS HTML scraper for map viewer URL."""

import json
import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.graph.state import AgentState

logger = logging.getLogger(__name__)

SCRAPE_TIMEOUT = 10.0  # seconds


def _find_first_layers_doc(state: AgentState):
    """Return the first retrieved doc with has_layers=True, or None."""
    for doc in state["retrieved_docs"]:
        if doc.metadata.get("has_layers", False):
            return doc
    return None


def _extract_layer_info(doc) -> tuple[str | None, list[dict]]:
    """Parse full_metadata JSON to get first layer id and its fields."""
    raw = doc.metadata.get("full_metadata")
    if not raw:
        return None, []
    try:
        full = json.loads(raw)
        layers = full.get("layers", [])
        if not layers:
            return None, []
        first_layer = layers[0]
        return str(first_layer.get("id", 0)), first_layer.get("fields", [])
    except (json.JSONDecodeError, KeyError):
        logger.warning("scraper: failed to parse full_metadata JSON")
        return None, []


async def scraper(state: AgentState) -> dict[str, Any]:
    """Scrape map viewer URL from the first has_layers doc's ArcGIS page."""
    doc = _find_first_layers_doc(state)
    if doc is None:
        logger.warning("scraper: no document with has_layers=True found")
        return {"scraped_layer_data": None, "map_viewer_url": None}

    base_url = doc.metadata.get("base_url", "")
    layer_id, fields = _extract_layer_info(doc)

    if not base_url or layer_id is None:
        logger.warning("scraper: missing base_url or layer_id — skipping scrape")
        return {"scraped_layer_data": None, "map_viewer_url": None}

    # Build the ArcGIS layer page URL (HTML, not JSON)
    layer_page_url = f"{base_url}/{layer_id}"

    map_viewer_url = None
    try:
        async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(layer_page_url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        view_section = soup.find(id="viewInSection")
        if view_section:
            anchor = view_section.find("a", href=True)
            if anchor:
                map_viewer_url = anchor["href"]
            else:
                logger.warning("scraper: #viewInSection found but no <a> tag inside")
        else:
            logger.warning(
                "scraper: #viewInSection element not found on %s", layer_page_url
            )
    except httpx.HTTPError as exc:
        logger.warning("scraper: HTTP error fetching %s — %s", layer_page_url, exc)

    scraped_layer_data = {
        "service_name": doc.metadata.get("service_name"),
        "base_url": base_url,
        "layer_id": layer_id,
        "fields": fields,
        "layer_page_url": layer_page_url,
    }

    return {
        "scraped_layer_data": scraped_layer_data,
        "map_viewer_url": map_viewer_url,
    }
