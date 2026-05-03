"""ingest_denver_public_schools.py

Read the Denver public schools geojson and ingest one Document per school
into the existing denver_gis_catalog Qdrant collection. Templated NL summaries
— no LLM calls.

Source: Denver Open Data Catalog "Public School 2025-2026" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/geospatialDenver::public-school-2025-2026/about
The geojson is gitignored; download manually into data/ before running.

Companion to ingest_denver_non_public_schools.py. Both share
doc_type=denver_school and are differentiated via metadata.institution_type
("public" here, "non-public" in the sibling script). Either dataset can
be retrieved or filtered independently.

Field naming notes:
  - The source SCHOOL_TYPE column ("District" / "Charter" / "Traditional" /
    "Traditional / Alternative") is preserved as metadata.school_type to
    match the source schema 1:1.
  - metadata.institution_type = "public" is our own dataset-origin field.
  - SCH_NAME_FULL (e.g. "John F. Kennedy High School") is preferred over
    SCHOOL_NAME (e.g. "John F. Kennedy") for the lead sentence and
    display_name, since it reads more naturally and gives the LLM more
    context. SCHOOL_NAME falls back when SCH_NAME_FULL is missing. Both
    fields are preserved in metadata.
  - NUM_STUDENTS is never populated in this dataset (0/232), so the
    grades sentence won't include enrollment for public-school docs.
    Field is still preserved in metadata in case it gets backfilled later.

URL plumbing follows the non-public-schools / libraries pattern (Point
geometry; coords from geometry.coordinates; per-school Google Maps
deep links for map_url).

Run:
  python scripts/ingest_denver_public_schools.py            # append (refuses if docs exist)
  python scripts/ingest_denver_public_schools.py --purge    # delete prior docs first
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
GEOJSON_GLOB = "ODC_EDU_PUBLICSCHOOLS_P_*.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Public Schools"
DOC_TYPE = "denver_school"
INSTITUTION_TYPE = "public"

GMAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "geospatialDenver::public-school-2025-2026/about"
)


def find_geojson(data_dir: Path = DATA_DIR) -> Path | None:
    matches = sorted(data_dir.glob(GEOJSON_GLOB))
    return matches[0] if matches else None


def load_features(path: Path) -> list[dict]:
    with path.open() as f:
        gj = json.load(f)
    return gj.get("features", [])


def _clean(value) -> str | None:
    """Normalize a string field: strips whitespace, treats empty strings
    as None. Handles the whitespace-only NOTES values seen in the source."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _article_for(word: str) -> str:
    """Return 'an' before vowel-sound first letters, else 'a'. All four
    SCHOOL_TYPE values seen in the public-schools dataset start with
    consonants ('District', 'Charter', 'Traditional', ...), so this
    heuristic works cleanly without special-casing."""
    if not word:
        return "a"
    return "an" if word[0].lower() in "aeiou" else "a"


def get_display_name(props: dict) -> str:
    """Pick the best name for the lead sentence and display_name. Prefers
    SCH_NAME_FULL (more verbose, more readable) and falls back to
    SCHOOL_NAME, then to a generic default."""
    return (
        _clean(props.get("SCH_NAME_FULL"))
        or _clean(props.get("SCHOOL_NAME"))
        or "School"
    )


def format_address(props: dict) -> str | None:
    line1 = _clean(props.get("ADDRESS_LINE1"))
    if not line1:
        return None
    line2 = _clean(props.get("ADDRESS_LINE2"))
    city = _clean(props.get("CITY")) or "Denver"
    state = _clean(props.get("STATE")) or "CO"
    zip_code = _clean(props.get("ZIP"))

    street = f"{line1}, {line2}" if line2 else line1
    tail = f"{city}, {state}"
    if zip_code:
        tail = f"{tail} {zip_code}"
    return f"{street}, {tail}"


def build_lead_sentence(props: dict) -> str:
    """Name + institution type + address. Always returns a sentence; falls
    back gracefully when school_type or address is missing."""
    name = get_display_name(props)
    school_type = _clean(props.get("SCHOOL_TYPE"))
    address = format_address(props)

    if school_type:
        type_lower = school_type.lower()
        descriptor = f"{_article_for(type_lower)} {type_lower} school"
    else:
        descriptor = "a public school"

    if address:
        return f"{name} is {descriptor} located at {address}."
    return f"{name} is {descriptor} in Denver."


def build_grades_sentence(props: dict) -> str | None:
    """Combine GRADE_LEVELS and NUM_STUDENTS into one sentence. NUM_STUDENTS
    is never populated for public schools today, but the conditional logic
    is preserved so the same code path works if it gets backfilled."""
    grades = _clean(props.get("GRADE_LEVELS"))
    try:
        students = int(props.get("NUM_STUDENTS")) if props.get("NUM_STUDENTS") is not None else None
    except (TypeError, ValueError):
        students = None
    has_students = bool(students and students > 0)

    if grades and has_students:
        return f"It serves grades {grades} with {students:,} students enrolled."
    if grades:
        return f"It serves grades {grades}."
    if has_students:
        return f"Enrollment: {students:,} students."
    return None


def build_district_sentence(district: str | None) -> str | None:
    cleaned = _clean(district)
    if not cleaned:
        return None
    return f"Part of the {cleaned} school district."


def build_website_sentence(website: str | None) -> str | None:
    cleaned = _clean(website)
    if not cleaned:
        return None
    return f"Website: {cleaned}."


def build_notes_sentence(notes: str | None) -> str | None:
    cleaned = _clean(notes)
    if not cleaned:
        return None
    return f"Notes: {cleaned}"


def build_page_content(props: dict) -> str:
    """Compose the embedded NL text from a single feature's properties."""
    sentences: list[str] = [build_lead_sentence(props)]
    for sentence in (
        build_grades_sentence(props),
        build_district_sentence(props.get("SCHOOL_DISTRICT")),
        build_website_sentence(props.get("WEBSITE")),
        build_notes_sentence(props.get("NOTES")),
    ):
        if sentence:
            sentences.append(sentence)
    return " ".join(sentences)


def build_school_document(feature: dict) -> Document | None:
    """Turn one geojson feature into a Document, or None if it lacks the
    Point geometry we need for the per-school map URL."""
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or len(coords) < 2:
        logger.warning(
            "skipping school %r — missing geometry coordinates",
            props.get("SCHOOL_NAME"),
        )
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        logger.warning(
            "skipping school %r — invalid coordinates %r",
            props.get("SCHOOL_NAME"), coords,
        )
        return None

    name = get_display_name(props)
    page_content = build_page_content(props)
    gmaps_url = GMAPS_URL_TEMPLATE.format(lat=lat, lon=lon)

    full_metadata = {
        "school_name": props.get("SCHOOL_NAME"),
        "sch_name_full": props.get("SCH_NAME_FULL"),
        "school_district": props.get("SCHOOL_DISTRICT"),
        "school_number": props.get("SCHOOL_NUMBER"),
        "school_type": props.get("SCHOOL_TYPE"),
        "school_level": props.get("SCHOOL_LEVEL"),
        "grade_levels": props.get("GRADE_LEVELS"),
        "num_students": props.get("NUM_STUDENTS"),
        "address_line1": props.get("ADDRESS_LINE1"),
        "address_line2": props.get("ADDRESS_LINE2"),
        "city": props.get("CITY"),
        "state": props.get("STATE"),
        "zip": props.get("ZIP"),
        "website": props.get("WEBSITE"),
        "notes": props.get("NOTES"),
        "last_verified": props.get("LAST_VERIFIED"),
        "address_id": props.get("ADDRESS_ID"),
        "globalid": props.get("GLOBALID"),
        "objectid": props.get("OBJECTID"),
    }

    return Document(
        page_content=page_content,
        metadata={
            "doc_type": DOC_TYPE,
            "institution_type": INSTITUTION_TYPE,
            "school_name": props.get("SCHOOL_NAME"),
            "sch_name_full": props.get("SCH_NAME_FULL"),
            "school_district": props.get("SCHOOL_DISTRICT"),
            "school_number": props.get("SCHOOL_NUMBER"),
            "school_type": props.get("SCHOOL_TYPE"),
            "school_level": props.get("SCHOOL_LEVEL"),
            "grade_levels": props.get("GRADE_LEVELS"),
            "num_students": props.get("NUM_STUDENTS"),
            "address_line1": props.get("ADDRESS_LINE1"),
            "city": props.get("CITY"),
            "zip": props.get("ZIP"),
            "website": props.get("WEBSITE"),
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
        doc = build_school_document(feature)
        if doc is not None:
            docs.append(doc)
    return docs


def _doc_filter() -> Filter:
    """Filters to public school docs only — matches both doc_type and
    institution_type so the non-public-schools ingest's docs are unaffected
    by a --purge run on this script."""
    return Filter(
        must=[
            FieldCondition(key="metadata.doc_type", match=MatchValue(value=DOC_TYPE)),
            FieldCondition(
                key="metadata.institution_type", match=MatchValue(value=INSTITUTION_TYPE)
            ),
        ]
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
    logger.info(
        "purged existing %s (institution_type=%s) points from %s",
        DOC_TYPE, INSTITUTION_TYPE, COLLECTION_NAME,
    )


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
        help=(
            f"Delete existing {DOC_TYPE} docs with institution_type="
            f"{INSTITUTION_TYPE!r} before insert. Non-public school docs are unaffected."
        ),
    )
    args = parser.parse_args()

    geojson_path = find_geojson()
    if geojson_path is None:
        logger.error(
            "no public schools geojson found in %s matching %s",
            DATA_DIR, GEOJSON_GLOB,
        )
        return 1
    logger.info("reading %s", geojson_path.name)

    features = load_features(geojson_path)
    logger.info("loaded %d public school features", len(features))

    docs = build_documents(features)
    logger.info(
        "built %d public school documents (skipped %d for missing geometry)",
        len(docs), len(features) - len(docs),
    )

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    existing = _count_existing(client)
    if existing > 0:
        if args.purge:
            _purge_existing(client)
        else:
            logger.error(
                "found %d existing public school points in %s — re-run with --purge to replace",
                existing, COLLECTION_NAME,
            )
            return 1

    upsert_to_qdrant(docs)
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
