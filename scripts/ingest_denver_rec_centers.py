"""ingest_denver_rec_centers.py

Read the Denver Parks & Recreation rec center geojson and ingest one Document
per rec center into the existing denver_gis_catalog Qdrant collection.
Templated NL summaries — no LLM calls.

Source: Denver Open Data Catalog "Recreation Centers" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/geospatialDenver::recreation-centers/about
The geojson is gitignored; download manually into data/ before running.

URL plumbing follows the parks pattern:
  - service_name = "Denver Recreation Centers" (constant — sources panel
    collapses to one citation entry per dataset).
  - base_url = hub_url = dataset hub /about page.
  - display_name = REC_NAME — drives unique map_viewer labels.
  - map_url = per-rec-center Google Maps deep link from LATITUDE/LONGITUDE
    properties (which are reliably populated 31/31 in the source data).

Run:
  python scripts/ingest_denver_rec_centers.py            # append (refuses if docs exist)
  python scripts/ingest_denver_rec_centers.py --purge    # delete prior docs first
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
GEOJSON_GLOB = "ODC_PARK_RECCENTER_P_*.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Recreation Centers"
DOC_TYPE = "denver_rec_center"

GMAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "geospatialDenver::recreation-centers/about"
)

# The source data uses the literal string "<Null>" in some text fields where
# a real null/None would be expected. Normalize on read.
LITERAL_NULL = "<Null>"


def find_geojson(data_dir: Path = DATA_DIR) -> Path | None:
    matches = sorted(data_dir.glob(GEOJSON_GLOB))
    return matches[0] if matches else None


def load_features(path: Path) -> list[dict]:
    with path.open() as f:
        gj = json.load(f)
    return gj.get("features", [])


def _clean(value) -> str | None:
    """Normalize a string field: strips whitespace, treats the literal
    "<Null>" sentinel and empty strings as None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == LITERAL_NULL:
        return None
    return s


def format_address(props: dict) -> str | None:
    """Compose a single-line readable address. Returns None if line1 is
    missing — without a street address there's nothing useful to render.
    ZIP is intentionally omitted because it's never populated in the source."""
    line1 = _clean(props.get("ADDRESS_LINE1"))
    if not line1:
        return None
    line2 = _clean(props.get("ADDRESS_LINE2"))
    city = _clean(props.get("CITY")) or "Denver"
    state = _clean(props.get("STATE")) or "CO"

    street = f"{line1}, {line2}" if line2 else line1
    return f"{street}, {city}, {state}"


def build_lead_sentence(props: dict) -> str:
    """Name + type + address. Always returns a sentence — falls back to
    generic phrasing when type or address is missing."""
    name = _clean(props.get("REC_NAME")) or "Recreation Center"
    rec_type = _clean(props.get("REC_TYPE"))
    descriptor = (
        f"a {rec_type.lower()} recreation center" if rec_type else "a recreation center"
    )
    address = format_address(props)
    if address:
        return (
            f"{name} is {descriptor} operated by Denver Parks & Recreation, "
            f"located at {address}."
        )
    return f"{name} is {descriptor} operated by Denver Parks & Recreation in Denver."


def build_phone_sentence(props: dict) -> str | None:
    phone = _clean(props.get("PHONE"))
    if not phone:
        return None
    return f"Phone: {phone}."


def build_building_sentence(props: dict) -> str | None:
    """Combine sqft + year_built + (optionally) year_remodel into one
    sentence. Skips the remodel clause when value is empty or 'Unknown'.
    Returns None when neither size nor year-built is available."""
    try:
        sqft = int(props.get("BLDG_SQFT")) if props.get("BLDG_SQFT") is not None else None
    except (TypeError, ValueError):
        sqft = None
    year_built = _clean(props.get("YEAR_BUILT"))
    year_remodel_raw = _clean(props.get("YEAR_REMODEL"))
    year_remodel = (
        year_remodel_raw
        if year_remodel_raw and year_remodel_raw.lower() != "unknown"
        else None
    )

    has_sqft = bool(sqft and sqft > 0)
    if not has_sqft and not year_built:
        return None

    if has_sqft and year_built:
        lead = f"The {sqft:,}-square-foot building was built in {year_built}"
    elif has_sqft:
        lead = f"The building is {sqft:,} square feet"
    else:
        lead = f"The building was built in {year_built}"

    if year_remodel:
        return f"{lead} and remodeled in {year_remodel}."
    return f"{lead}."


def build_facilities_sentence(facilities: str | None) -> str | None:
    """Pass MARKETED_FACILITIES through verbatim. Same convention as parks
    FACILITIES — the city's data team curates the wording; reformatting
    introduces drift."""
    cleaned = _clean(facilities)
    if not cleaned:
        return None
    return f"Marketed facilities include: {cleaned}."


def build_programs_sentence(programs: str | None) -> str | None:
    cleaned = _clean(programs)
    if not cleaned:
        return None
    return f"Programs offered: {cleaned}."


def build_page_content(props: dict) -> str:
    """Compose the embedded NL text from a single feature's properties."""
    sentences: list[str] = [build_lead_sentence(props)]
    for sentence in (
        build_phone_sentence(props),
        build_building_sentence(props),
        build_facilities_sentence(props.get("MARKETED_FACILITIES")),
        build_programs_sentence(props.get("MARKETED_PROGRAMS")),
    ):
        if sentence:
            sentences.append(sentence)
    return " ".join(sentences)


def build_rec_center_document(feature: dict) -> Document | None:
    """Turn one geojson feature into a Document, or None if it lacks the
    coordinates we need to build the per-rec-center map URL. Coordinates
    come from the LATITUDE/LONGITUDE properties (parks pattern), which are
    reliably populated 31/31."""
    props = feature.get("properties") or {}
    try:
        lat = float(props["LATITUDE"])
        lon = float(props["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "skipping rec center %r — missing/invalid LATITUDE/LONGITUDE",
            props.get("REC_NAME"),
        )
        return None

    name = _clean(props.get("REC_NAME")) or "Recreation Center"
    page_content = build_page_content(props)
    gmaps_url = GMAPS_URL_TEMPLATE.format(lat=lat, lon=lon)

    full_metadata = {
        "rec_name": name,
        "loc_code": props.get("LOC_CODE"),
        "rec_type": props.get("REC_TYPE"),
        "marketing_code": props.get("MARKETING_CODE"),
        "address_line1": props.get("ADDRESS_LINE1"),
        "address_line2": _clean(props.get("ADDRESS_LINE2")),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
        "phone": props.get("PHONE"),
        "marketed_facilities": props.get("MARKETED_FACILITIES"),
        "marketed_programs": props.get("MARKETED_PROGRAMS"),
        "programs_link": props.get("PROGRAMS_LINK"),
        "pool_hours_link": props.get("POOL_HOURS_LINK"),
        "news_link": props.get("NEWS_LINK"),
        "hours": props.get("HOURS"),
        "photo": props.get("PHOTO"),
        "year_built": props.get("YEAR_BUILT"),
        "year_remodel": props.get("YEAR_REMODEL"),
        "bldg_sqft": props.get("BLDG_SQFT"),
        "label": props.get("LABEL"),
        "facilities_old": props.get("FACILITIES_OLD"),
        "globalid": props.get("GLOBALID"),
        "objectid": props.get("OBJECTID"),
    }

    return Document(
        page_content=page_content,
        metadata={
            "doc_type": DOC_TYPE,
            "rec_name": name,
            "loc_code": props.get("LOC_CODE"),
            "rec_type": props.get("REC_TYPE"),
            "address_line1": props.get("ADDRESS_LINE1"),
            "city": props.get("CITY"),
            "phone": props.get("PHONE"),
            "year_built": props.get("YEAR_BUILT"),
            "bldg_sqft": props.get("BLDG_SQFT"),
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
        doc = build_rec_center_document(feature)
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
            "no rec centers geojson found in %s matching %s",
            DATA_DIR, GEOJSON_GLOB,
        )
        return 1
    logger.info("reading %s", geojson_path.name)

    features = load_features(geojson_path)
    logger.info("loaded %d rec center features", len(features))

    docs = build_documents(features)
    logger.info("built %d rec center documents (skipped %d for missing coords)",
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
