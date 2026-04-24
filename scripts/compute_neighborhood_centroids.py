"""compute_neighborhood_centroids.py

Reads data/ODC_POP_ACS20172021NBRHDCOMMON.geojson and computes the geographic
centroid (lat/lon) of each neighborhood polygon via shapely. Patches each
neighborhood's five Qdrant chunks (population/housing/education/income_poverty/
demographics) with a `metadata.location = {"lat": float, "lon": float}` field —
the payload shape Qdrant's geo filter expects.

Idempotent: rerunning overwrites the `location` field in place. Other metadata
is untouched. The GeoJSON CRS is EPSG:4326 (WGS84), so centroid coordinates
are already in lat/lon.

Run: python scripts/compute_neighborhood_centroids.py
"""

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from shapely.geometry import shape

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
GEOJSON_PATH = BASE_DIR / "data" / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")


def compute_centroids(geojson_path: Path) -> dict[str, dict[str, float]]:
    """Returns {NBHD_NAME: {"lat": <EPSG:4326 y>, "lon": <EPSG:4326 x>}}.

    Shapely convention on EPSG:4326 geometries: point.x = longitude, point.y =
    latitude. This is the opposite of how most APIs order lat/lon, so be deliberate.
    """
    with geojson_path.open() as f:
        data = json.load(f)

    out: dict[str, dict[str, float]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = props.get("NBHD_NAME")
        geom = feature.get("geometry")
        if not name or not geom:
            logger.warning("feature missing name or geometry: name=%r", name)
            continue
        try:
            poly = shape(geom)
            c = poly.centroid
        except Exception as exc:
            logger.warning("centroid computation failed for %s: %s", name, exc)
            continue
        out[name] = {"lat": c.y, "lon": c.x}
    return out


def _filter_by_name(name: str) -> Filter:
    return Filter(
        must=[
            FieldCondition(
                key="metadata.neighborhood_name",
                match=MatchValue(value=name),
            )
        ]
    )


def count_matching_points(client: QdrantClient, name: str) -> int:
    """Scroll through all points for this neighborhood and count them."""
    total = 0
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=_filter_by_name(name),
            limit=100,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        total += len(points)
        if next_offset is None:
            break
    return total


def main() -> int:
    if not GEOJSON_PATH.exists():
        logger.error("geojson not found at %s", GEOJSON_PATH)
        return 1

    centroids = compute_centroids(GEOJSON_PATH)
    logger.info("computed centroids for %d neighborhoods", len(centroids))

    # Sanity-print a couple samples so the user can eyeball the axes.
    for sample_name in list(centroids.keys())[:3]:
        loc = centroids[sample_name]
        logger.info(
            "sample: %s → lat=%.6f lon=%.6f (Denver should be ~lat 39.7, lon -104.9)",
            sample_name, loc["lat"], loc["lon"],
        )

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

    patched_neighborhoods = 0
    zero_match = []
    failed = []
    total_points = 0

    for name, loc in centroids.items():
        match_count = count_matching_points(client, name)
        if match_count == 0:
            zero_match.append(name)
            continue

        try:
            client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"location": loc},
                points=_filter_by_name(name),
                key="metadata",
            )
        except Exception as exc:
            logger.warning("set_payload failed for %s: %s", name, exc)
            failed.append(name)
            continue

        logger.info(
            "%s → lat=%.4f lon=%.4f  (patched %d chunks)",
            name, loc["lat"], loc["lon"], match_count,
        )
        patched_neighborhoods += 1
        total_points += match_count

    logger.info(
        "done — patched %d neighborhoods covering %d points",
        patched_neighborhoods, total_points,
    )
    if zero_match:
        logger.warning(
            "%d neighborhoods had no matching Qdrant points (name mismatch?): %s",
            len(zero_match), zero_match,
        )
    if failed:
        logger.warning("%d neighborhoods failed to patch: %s", len(failed), failed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
