"""ingest_denver_libraries.py

Read the Denver Public Library branch geojson and ingest one Document per
library into the existing denver_gis_catalog Qdrant collection. Templated
NL summaries — no LLM calls.

Source: Denver Open Data Catalog "Library" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/3d044de34a8049d9b90ed8e5720e3991/about
The geojson is gitignored; download manually into data/ before running.

URL plumbing follows the parks pattern:
  - service_name = "Denver Libraries" (constant — sources panel collapses
    to one citation entry per dataset).
  - base_url = hub_url = dataset hub /about page.
  - display_name = library name — drives unique map_viewer labels.
  - map_url = per-library Google Maps deep link (parks pattern). Each
    branch surfaces its own clickable pin in map_viewer.

Run:
  python scripts/ingest_denver_libraries.py            # append (refuses if docs exist)
  python scripts/ingest_denver_libraries.py --purge    # delete prior docs first
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
GEOJSON_GLOB = "ODC_SVCS_LIBRARY_P_*.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Libraries"
DOC_TYPE = "denver_library"

GMAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "3d044de34a8049d9b90ed8e5720e3991/about"
)


def find_geojson(data_dir: Path = DATA_DIR) -> Path | None:
    matches = sorted(data_dir.glob(GEOJSON_GLOB))
    return matches[0] if matches else None


def load_features(path: Path) -> list[dict]:
    with path.open() as f:
        gj = json.load(f)
    return gj.get("features", [])


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_address(props: dict) -> str | None:
    """Compose a single-line readable address. Returns None if line1 is
    missing — without a street address there's nothing useful to render."""
    line1 = (props.get("ADDRESS_LINE1") or "").strip() or None
    if not line1:
        return None
    line2 = (props.get("ADDRESS_LINE2") or "").strip() or None
    city = (props.get("CITY") or "").strip() or "Denver"
    state = (props.get("STATE") or "").strip() or "CO"
    zip_code = (props.get("ZIP") or "").strip()

    street = f"{line1}, {line2}" if line2 else line1
    tail = f"{city}, {state}"
    if zip_code:
        tail = f"{tail} {zip_code}"
    return f"{street}, {tail}"


def build_activity_sentence(props: dict) -> str | None:
    """Combine building size, circulation, and door-count into one sentence
    when at least one is present and positive. Returns None when nothing
    useful is available, so the page_content cleanly omits the line."""
    sqft = _as_int(props.get("TOTAL_BLDG_SQFT"))
    circulation = _as_int(props.get("CIRCULATION"))
    door_count = _as_int(props.get("DOOR_COUNT"))

    has_sqft = bool(sqft and sqft > 0)
    activity_phrases: list[str] = []
    if circulation and circulation > 0:
        activity_phrases.append(f"{circulation:,} circulation events")
    if door_count and door_count > 0:
        activity_phrases.append(f"{door_count:,} door visits")

    if not has_sqft and not activity_phrases:
        return None

    if has_sqft and activity_phrases:
        return (
            f"The {sqft:,}-square-foot building recorded "
            f"{' and '.join(activity_phrases)} in the most recent reporting period."
        )
    if has_sqft:
        return f"The branch occupies a {sqft:,}-square-foot building."
    return (
        f"The branch recorded {' and '.join(activity_phrases)} "
        f"in the most recent reporting period."
    )


def build_status_sentence(status: str | None) -> str | None:
    """Surface STATUS verbatim when populated. The source data sometimes
    truncates these mid-word (a known upstream limitation) — emit anyway
    since even partial advisories are user-relevant."""
    text = (status or "").strip()
    if not text:
        return None
    return f"Status: {text}"


def build_page_content(props: dict) -> str:
    """Compose the embedded NL text from a single feature's properties."""
    name = (props.get("LIBRARY_NAME") or "").strip() or "Denver Library"
    address = format_address(props)
    if address:
        lead = f"{name} is a Denver Public Library branch located at {address}."
    else:
        lead = f"{name} is a Denver Public Library branch in Denver."

    sentences = [lead]
    activity = build_activity_sentence(props)
    if activity:
        sentences.append(activity)
    status = build_status_sentence(props.get("STATUS"))
    if status:
        sentences.append(status)
    return " ".join(sentences)


def build_library_document(feature: dict) -> Document | None:
    """Turn one geojson feature into a Document, or None if it lacks the
    point geometry we need for the per-library map URL. Coordinates come
    from feature.geometry.coordinates ([lon, lat]), not from properties."""
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or len(coords) < 2:
        logger.warning(
            "skipping library %r — missing geometry coordinates",
            props.get("LIBRARY_NAME"),
        )
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        logger.warning(
            "skipping library %r — invalid coordinates %r",
            props.get("LIBRARY_NAME"), coords,
        )
        return None

    name = (props.get("LIBRARY_NAME") or "").strip() or "Denver Library"
    page_content = build_page_content(props)
    gmaps_url = GMAPS_URL_TEMPLATE.format(lat=lat, lon=lon)

    full_metadata = {
        "library_name": name,
        "branch_code": props.get("BRANCH_CODE"),
        "address_line1": props.get("ADDRESS_LINE1"),
        "address_line2": props.get("ADDRESS_LINE2"),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
        "zip": props.get("ZIP"),
        "door_count": props.get("DOOR_COUNT"),
        "circulation": props.get("CIRCULATION"),
        "total_bldg_sqft": props.get("TOTAL_BLDG_SQFT"),
        "status": props.get("STATUS"),
        "globalid": props.get("GLOBALID"),
        "address_id": props.get("ADDRESS_ID"),
        "objectid": props.get("OBJECTID"),
    }

    return Document(
        page_content=page_content,
        metadata={
            "doc_type": DOC_TYPE,
            "library_name": name,
            "branch_code": props.get("BRANCH_CODE"),
            "address_line1": props.get("ADDRESS_LINE1"),
            "city": props.get("CITY"),
            "zip": props.get("ZIP"),
            "door_count": props.get("DOOR_COUNT"),
            "circulation": props.get("CIRCULATION"),
            "total_bldg_sqft": props.get("TOTAL_BLDG_SQFT"),
            "status": props.get("STATUS"),
            "globalid": props.get("GLOBALID"),
            "objectid": props.get("OBJECTID"),
            "location": {"lat": lat, "lon": lon},
            "service_name": SERVICE_NAME,
            "display_name": name,
            "base_url": DATASET_HUB_URL,
            "hub_url": DATASET_HUB_URL,
            "map_url": gmaps_url,
            "has_layers": False,
            "full_metadata": json.dumps(full_metadata, default=str),
        },
    )


def build_documents(features: list[dict]) -> list[Document]:
    docs: list[Document] = []
    for feature in features:
        doc = build_library_document(feature)
        if doc is not None:
            docs.append(doc)
    return docs


def _doc_filter() -> Filter:
    return Filter(
        must=[FieldCondition(key="metadata.doc_type", match=MatchValue(value=DOC_TYPE))]
    )


def _count_existing(client: QdrantClient) -> int:
    total = 0
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=_doc_filter(),
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
    client.delete(collection_name=COLLECTION_NAME, points_selector=_doc_filter())
    logger.info("purged existing %s points from %s", DOC_TYPE, COLLECTION_NAME)


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
        help=f"Delete existing {DOC_TYPE} Qdrant points before insert.",
    )
    args = parser.parse_args()

    geojson_path = find_geojson()
    if geojson_path is None:
        logger.error(
            "no libraries geojson found in %s matching %s",
            DATA_DIR, GEOJSON_GLOB,
        )
        return 1
    logger.info("reading %s", geojson_path.name)

    features = load_features(geojson_path)
    logger.info("loaded %d library features", len(features))

    docs = build_documents(features)
    logger.info("built %d library documents (skipped %d for missing geometry)",
                len(docs), len(features) - len(docs))

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    existing = _count_existing(client)
    if existing > 0:
        if args.purge:
            _purge_existing(client)
        else:
            logger.error(
                "found %d existing %s points in %s — re-run with --purge to replace",
                existing, DOC_TYPE, COLLECTION_NAME,
            )
            return 1

    upsert_to_qdrant(docs)
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
