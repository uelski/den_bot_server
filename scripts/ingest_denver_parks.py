"""ingest_denver_parks.py

Read the Denver Parks geojson (from data/ODC_PARK_PARKLAND_A_*.geojson) and
ingest one Document per park into the existing denver_gis_catalog Qdrant
collection. Uses templated NL summaries — no LLM calls.

Source: Denver Open Data Catalog "Park Boundary" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/denvergov::park-boundary
The geojson is gitignored; download manually into data/ before running.

URL plumbing notes:
  - hub_url is the per-park Google Maps deep link built from LATITUDE/LONGITUDE.
    The map_viewer SSE event dedups by hub_url, so this gives one clickable
    map link per park surfaced.
  - base_url is the dataset-level ArcGIS hub URL (constant across all parks);
    it surfaces in the sources event as a single citation entry.

Run:
  python scripts/ingest_denver_parks.py            # append (refuses if denver_park docs exist)
  python scripts/ingest_denver_parks.py --purge    # delete prior denver_park docs first
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
GEOJSON_GLOB = "ODC_PARK_PARKLAND_A_*.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Parks"
DOC_TYPE = "denver_park"

# Per-park: Google Maps search by coordinates. Renders as the park's location.
GMAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"
# Constant across all parks: dataset-level ArcGIS Hub page (citation source).
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "e752556524024fbfac700b35f33c0f2c/about"
)


def find_geojson(data_dir: Path = DATA_DIR) -> Path | None:
    """Return the parks geojson path, or None if absent. Glob lets us tolerate
    re-downloads with different cache-busting suffixes in the filename."""
    matches = sorted(data_dir.glob(GEOJSON_GLOB))
    return matches[0] if matches else None


def load_features(path: Path) -> list[dict]:
    with path.open() as f:
        gj = json.load(f)
    return gj.get("features", [])


def build_descriptor(park_type: str, park_class: str) -> str:
    """Produce a natural-language descriptor combining PARK_TYPE and PARK_CLASS.

    Rules (locked during template workshop, cover all 7×12 combinations seen
    in the 374-record dataset):
      - PARK_TYPE == "Park" (288/374): "{class} park" — handles "neighborhood
        park", "linear park", "mountain park", etc.
      - PARK_TYPE in {"Grounds", "Golf", "Facility"}: use class only — these
        types are system-internal labels and the class names the actual
        facility (e.g., "Recreation Center" beats "Recreation Center grounds").
      - PARK_TYPE == PARK_CLASS (case-insensitive): use one — avoids stutter
        like "open space open space".
      - Otherwise: "{class} {type}" — covers cases like
        "pocket conservation tract".
    """
    pt = (park_type or "").strip()
    pc = (park_class or "").strip()
    if not pt and not pc:
        return "park"

    if pt == "Park":
        return f"{pc.lower()} park" if pc else "park"
    if pt in ("Grounds", "Golf", "Facility"):
        return pc.lower() if pc else pt.lower()
    if pt.lower() == pc.lower():
        return pt.lower()
    return f"{pc.lower()} {pt.lower()}".strip()


def build_locator_sentence(address: str | None, cross: str | None) -> str | None:
    """Choose the most specific locator phrasing available. None if neither
    field is populated (~10% of records lack both)."""
    address = (address or "").strip() or None
    cross = (cross or "").strip() or None
    if address and cross:
        return f"Located at {address}, near {cross}."
    if address:
        return f"Located at {address}."
    if cross:
        return f"Located near {cross}."
    return None


def build_amenities_sentence(facilities: str | None) -> str | None:
    """FACILITIES is a free-text comma-separated list in the source data
    (~99% populated). Pass it through verbatim — the city's data team
    already curates the wording; reformatting it introduces drift."""
    facilities = (facilities or "").strip()
    if not facilities:
        return None
    return f"Amenities include: {facilities}."


def build_page_content(props: dict) -> str:
    """Compose the embedded NL text from a single feature's properties."""
    name = (props.get("FORMAL_NAME") or props.get("LOCATION") or "Unnamed Park").strip()
    acres_raw = props.get("GIS_ACRES")
    descriptor = build_descriptor(props.get("PARK_TYPE", ""), props.get("PARK_CLASS", ""))

    try:
        acres = float(acres_raw)
        lead = f"{name} is a {acres:.1f}-acre {descriptor} in Denver."
    except (TypeError, ValueError):
        lead = f"{name} is a {descriptor} in Denver."

    sentences: list[str] = [lead]
    locator = build_locator_sentence(props.get("ADDRESS_LINE1"), props.get("CROSS_STREETS"))
    if locator:
        sentences.append(locator)
    amenities = build_amenities_sentence(props.get("FACILITIES"))
    if amenities:
        sentences.append(amenities)
    return " ".join(sentences)


def build_park_document(feature: dict) -> Document | None:
    """Turn one geojson feature into a Document, or None if it lacks the
    coordinates we need to build the per-park map URL."""
    props = feature.get("properties") or {}
    try:
        lat = float(props["LATITUDE"])
        lon = float(props["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "skipping park %r — missing/invalid LATITUDE/LONGITUDE",
            props.get("FORMAL_NAME") or props.get("LOCATION"),
        )
        return None

    name = (props.get("FORMAL_NAME") or props.get("LOCATION") or "Unnamed Park").strip()
    page_content = build_page_content(props)

    gmaps_url = GMAPS_URL_TEMPLATE.format(lat=lat, lon=lon)

    full_metadata = {
        "formal_name": props.get("FORMAL_NAME"),
        "location_name": props.get("LOCATION"),
        "park_type": props.get("PARK_TYPE"),
        "park_class": props.get("PARK_CLASS"),
        "acres": props.get("GIS_ACRES"),
        "address_line1": props.get("ADDRESS_LINE1"),
        "cross_streets": props.get("CROSS_STREETS"),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
        "council_district": props.get("COUNCIL_DIST"),
        "maint_district": props.get("MAINT_DIST"),
        "police_district": props.get("POLICE_DIST"),
        "designated": props.get("DESIGNATED"),
        "master_plan": props.get("MASTER_PLAN"),
        "first_aq_date": props.get("FIRST_AQ_DATE"),
        "objectid": props.get("OBJECTID"),
        "globalid": props.get("GLOBALID"),
        "loc_code": props.get("LOC_CODE"),
        "facilities": props.get("FACILITIES"),
    }

    return Document(
        page_content=page_content,
        metadata={
            "doc_type": DOC_TYPE,
            "park_name": name,
            "formal_name": props.get("FORMAL_NAME"),
            "location_name": props.get("LOCATION"),
            "park_type": props.get("PARK_TYPE"),
            "park_class": props.get("PARK_CLASS"),
            "acres": props.get("GIS_ACRES"),
            "address_line1": props.get("ADDRESS_LINE1"),
            "cross_streets": props.get("CROSS_STREETS"),
            "council_district": props.get("COUNCIL_DIST"),
            "maint_district": props.get("MAINT_DIST"),
            "globalid": props.get("GLOBALID"),
            "objectid": props.get("OBJECTID"),
            "location": {"lat": lat, "lon": lon},
            "service_name": SERVICE_NAME,
            "base_url": DATASET_HUB_URL,
            "hub_url": gmaps_url,
            "has_layers": False,
            "full_metadata": json.dumps(full_metadata, default=str),
        },
    )


def build_documents(features: list[dict]) -> list[Document]:
    docs: list[Document] = []
    for feature in features:
        doc = build_park_document(feature)
        if doc is not None:
            docs.append(doc)
    return docs


def _denver_park_filter() -> Filter:
    return Filter(
        must=[FieldCondition(key="metadata.doc_type", match=MatchValue(value=DOC_TYPE))]
    )


def _count_existing(client: QdrantClient) -> int:
    flt = _denver_park_filter()
    total = 0
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=flt,
            limit=1000,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        total += len(points)
        if next_offset is None:
            break
    return total


def _purge_existing(client: QdrantClient) -> None:
    client.delete(collection_name=COLLECTION_NAME, points_selector=_denver_park_filter())
    logger.info("purged existing denver_park points from %s", COLLECTION_NAME)


def upsert_to_qdrant(docs: list[Document]) -> None:
    dense = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    sparse = FastEmbedSparse(model_name="Qdrant/bm25")
    store = QdrantVectorStore.from_existing_collection(
        embedding=dense,
        sparse_embedding=sparse,
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
    )
    logger.info("upserting %d documents into %s", len(docs), COLLECTION_NAME)
    store.add_documents(docs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing denver_park Qdrant points before insert.",
    )
    args = parser.parse_args()

    geojson_path = find_geojson()
    if geojson_path is None:
        logger.error(
            "no parks geojson found in %s matching %s",
            DATA_DIR, GEOJSON_GLOB,
        )
        return 1
    logger.info("reading %s", geojson_path.name)

    features = load_features(geojson_path)
    logger.info("loaded %d parks features", len(features))

    docs = build_documents(features)
    logger.info("built %d park documents (skipped %d for missing coords)",
                len(docs), len(features) - len(docs))

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    existing = _count_existing(client)
    if existing > 0:
        if args.purge:
            _purge_existing(client)
        else:
            logger.error(
                "found %d existing denver_park points in %s — re-run with --purge to replace",
                existing, COLLECTION_NAME,
            )
            return 1

    upsert_to_qdrant(docs)
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
