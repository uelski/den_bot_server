"""ingest_denver_crime.py

Read the Denver Open Data Catalog crime offenses CSV and ingest one Document
per neighborhood into the existing denver_gis_catalog Qdrant collection. Uses
templated NL summaries — no LLM calls.

Source: Denver Open Data Catalog "Crime" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/1e080d3ce2ae4e2698745a0d02345d4a_324/about
The CSV is gitignored (large); download manually into data/ before running.

Pipeline:
  1. Load CSV (≈355k rows), filter IS_CRIME=1, drop rows without GEO_LAT/GEO_LON.
  2. Window to the most recent 5 calendar years (anchored to max(REPORTED_DATE)
     in the data, not to wall-clock today — keeps the window populated even
     when the export is stale).
  3. Spatial-join each incident to a neighborhood polygon via STRtree + PIP
     against ODC_POP_ACS20172021NBRHDCOMMON.geojson. Drops incidents that fall
     outside any polygon (airport, edge effects).
  4. Aggregate per neighborhood: total count, 5-bucket category counts, top 5
     specific offense_type_id, yearly counts for YoY trend.
  5. Build templated NL summary per neighborhood and ingest as one Document.

Run:
  python scripts/ingest_denver_crime.py            # append (refuses if docs exist)
  python scripts/ingest_denver_crime.py --purge    # delete prior docs first
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

if TYPE_CHECKING:
    import pandas as pd

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CRIME_CSV = DATA_DIR / "ODC_CRIME_OFFENSES_P_6980386009720568002.csv"
NEIGHBORHOOD_GEOJSON = DATA_DIR / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Crime Statistics"
DOC_TYPE = "neighborhood_crime_summary"
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "1e080d3ce2ae4e2698745a0d02345d4a_324/about"
)
DATASET_EXPLORE_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "1e080d3ce2ae4e2698745a0d02345d4a_324/explore"
)

WINDOW_YEARS = 5
TOP_OFFENSE_COUNT = 5
TREND_THRESHOLD = 0.10  # ±10% endpoint-to-endpoint = "up"/"down"; otherwise "flat"

# Maps Denver's offense_category_id values into 5 high-level buckets.
# Unknown categories fall through to "other" (logged once per unknown value).
OFFENSE_CATEGORY_BUCKETS: dict[str, str] = {
    # violent: force or threat of force against a person
    "aggravated-assault": "violent",
    "murder": "violent",
    "robbery": "violent",
    "sexual-assault": "violent",
    "other-crimes-against-persons": "violent",
    # property: harm to property, no direct person-on-person force
    "arson": "property",
    "auto-theft": "property",
    "burglary": "property",
    "larceny": "property",
    "theft-from-motor-vehicle": "property",
    "white-collar-crime": "property",
    # drug
    "drug-alcohol": "drug",
    # public-disorder
    "public-disorder": "public-disorder",
    # catchall
    "all-other-crimes": "other",
}
BUCKET_ORDER = ["violent", "property", "drug", "public-disorder", "other"]


_unknown_categories_seen: set[str] = set()


def bucket_for_category(category_id: str) -> str:
    """Map a Denver offense_category_id to one of the 5 buckets. Unknown
    inputs fall back to 'other' and are logged once per distinct value so we
    notice if Denver introduces a new category."""
    if category_id in OFFENSE_CATEGORY_BUCKETS:
        return OFFENSE_CATEGORY_BUCKETS[category_id]
    if category_id and category_id not in _unknown_categories_seen:
        _unknown_categories_seen.add(category_id)
        logger.warning("unknown offense_category_id %r → bucketed as 'other'", category_id)
    return "other"


def compute_yoy_trend(year_counts: dict[int, int]) -> str:
    """Compare the oldest and newest year in the window. Returns 'up', 'down',
    or 'flat'. Single-year windows return 'flat' since there's nothing to
    compare. The newest year may be partial (data export cutoff mid-year);
    callers should mention the date range in the surrounding text so the
    reader can interpret the trend correctly."""
    if len(year_counts) < 2:
        return "flat"
    years_sorted = sorted(year_counts)
    oldest, newest = years_sorted[0], years_sorted[-1]
    start, end = year_counts[oldest], year_counts[newest]
    if start == 0:
        return "up" if end > 0 else "flat"
    delta = (end - start) / start
    if delta > TREND_THRESHOLD:
        return "up"
    if delta < -TREND_THRESHOLD:
        return "down"
    return "flat"


def top_offense_types(offense_type_ids: list[str], n: int = TOP_OFFENSE_COUNT) -> list[tuple[str, int]]:
    """Return the n most common offense_type_id values with their counts.
    Ties broken by Counter's insertion order (first occurrence wins)."""
    return Counter(offense_type_ids).most_common(n)


def _format_int(n: int) -> str:
    return f"{n:,}"


def _format_pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.0%"
    return f"{numerator / denominator * 100:.1f}%"


def build_page_content(
    *,
    neighborhood_name: str,
    total: int,
    start_year: int,
    end_year: int,
    bucket_counts: dict[str, int],
    top_offenses: list[tuple[str, int]],
    yoy_trend: str,
    year_counts: dict[int, int],
) -> str:
    """Compose the embedded NL text for one neighborhood. Factual phrasing —
    no 'high crime' / 'safe' verdicts; let the data speak."""
    lead = (
        f"{neighborhood_name} reported {_format_int(total)} criminal incidents "
        f"from {start_year} through {end_year}."
    )

    # Order the bucket sentence by descending count for readability.
    ordered_buckets = sorted(
        ((b, bucket_counts.get(b, 0)) for b in BUCKET_ORDER),
        key=lambda kv: kv[1],
        reverse=True,
    )
    bucket_phrases = [
        f"{label} ({_format_int(count)}, {_format_pct(count, total)})"
        for label, count in ordered_buckets
        if count > 0
    ]
    if bucket_phrases:
        bucket_sentence = "By category: " + ", ".join(bucket_phrases) + "."
    else:
        bucket_sentence = ""

    if top_offenses:
        offense_phrases = [
            f"{name} ({_format_int(count)})" for name, count in top_offenses
        ]
        top_sentence = (
            "The most common specific offenses were "
            + ", ".join(offense_phrases)
            + "."
        )
    else:
        top_sentence = ""

    if len(year_counts) >= 2 and total > 0:
        years_sorted = sorted(year_counts)
        oldest, newest = years_sorted[0], years_sorted[-1]
        trend_word = {"up": "increased", "down": "decreased", "flat": "stayed roughly flat"}[yoy_trend]
        trend_sentence = (
            f"Year-over-year, incident counts {trend_word} from "
            f"{_format_int(year_counts[oldest])} in {oldest} to "
            f"{_format_int(year_counts[newest])} in {newest}."
        )
    else:
        trend_sentence = ""

    parts = [lead, bucket_sentence, top_sentence, trend_sentence]
    return " ".join(p for p in parts if p)


def load_neighborhood_polygons(geojson_path: Path) -> tuple[list[str], list, list[dict]]:
    """Read the ACS neighborhoods geojson. Returns parallel lists:
      - names: NBHD_NAME for each feature
      - polygons: shapely geometries (used for both PIP and centroids)
      - centroids: {"lat": float, "lon": float} per feature

    Order matters — STRtree returns indices into the input list, so callers
    use the same index to look up name + centroid.
    """
    with geojson_path.open() as f:
        data = json.load(f)

    names: list[str] = []
    polygons: list = []
    centroids: list[dict] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = props.get("NBHD_NAME")
        geom = feature.get("geometry")
        if not name or not geom:
            continue
        try:
            poly = shape(geom)
            c = poly.centroid
        except Exception as exc:
            logger.warning("polygon parse failed for %s: %s", name, exc)
            continue
        names.append(name)
        polygons.append(poly)
        # shapely on EPSG:4326: point.x = lon, point.y = lat (deliberately flipped here).
        centroids.append({"lat": c.y, "lon": c.x})
    return names, polygons, centroids


def tag_neighborhoods(
    df, names: list[str], polygons: list
) -> "pd.Series":
    """Assign each row a neighborhood_name via STRtree-accelerated PIP.

    Returns a pandas Series aligned to df.index with the matched name or
    None for rows outside any polygon. Bulk tree query first narrows
    candidates to those whose bbox contains the point; then we run the
    polygon's contains() check on each candidate.
    """
    import pandas as pd

    tree = STRtree(polygons)
    name_for_idx: dict[int, str] = {i: names[i] for i in range(len(names))}

    out: list[str | None] = []
    for lat, lon in zip(df["GEO_LAT"], df["GEO_LON"]):
        pt = Point(lon, lat)  # shapely (x=lon, y=lat)
        candidate_idxs = tree.query(pt)
        matched: str | None = None
        for idx in candidate_idxs:
            if polygons[idx].contains(pt):
                matched = name_for_idx[int(idx)]
                break
        out.append(matched)
    return pd.Series(out, index=df.index, name="neighborhood_name")


def load_and_filter_crime(csv_path: Path) -> "pd.DataFrame":
    """Load, filter to IS_CRIME=1, drop missing coords, parse REPORTED_DATE,
    and trim to the most recent WINDOW_YEARS calendar years."""
    import pandas as pd

    logger.info("loading %s", csv_path.name)
    df = pd.read_csv(
        csv_path,
        encoding="utf-8-sig",  # the file has a BOM
        usecols=[
            "OFFENSE_TYPE_ID",
            "OFFENSE_CATEGORY_ID",
            "REPORTED_DATE",
            "GEO_LON",
            "GEO_LAT",
            "IS_CRIME",
        ],
        dtype={
            "OFFENSE_TYPE_ID": "string",
            "OFFENSE_CATEGORY_ID": "string",
            "REPORTED_DATE": "string",
        },
        low_memory=False,
    )
    initial = len(df)
    logger.info("loaded %s rows", _format_int(initial))

    df = df[df["IS_CRIME"] == 1]
    after_is_crime = len(df)
    logger.info("after IS_CRIME=1 filter: %s rows", _format_int(after_is_crime))

    df = df.dropna(subset=["GEO_LAT", "GEO_LON"])
    after_coords = len(df)
    logger.info("after dropping rows without coords: %s rows", _format_int(after_coords))

    df["REPORTED_DATE"] = pd.to_datetime(
        df["REPORTED_DATE"], format="%m/%d/%Y %I:%M:%S %p", errors="coerce"
    )
    df = df.dropna(subset=["REPORTED_DATE"])

    df["year"] = df["REPORTED_DATE"].dt.year.astype(int)
    max_year = int(df["year"].max())
    min_window_year = max_year - (WINDOW_YEARS - 1)
    df = df[df["year"] >= min_window_year]
    logger.info(
        "windowed to last %d years (%d–%d): %s rows",
        WINDOW_YEARS, min_window_year, max_year, _format_int(len(df)),
    )

    return df


def aggregate_per_neighborhood(df) -> dict[str, dict]:
    """Returns {neighborhood_name: stats_dict}. Each stats_dict has:
      total, year_range (start, end), bucket_counts, top_offenses (list of tuples),
      yoy_trend, year_counts.
    """
    out: dict[str, dict] = {}
    for name, group in df.groupby("neighborhood_name", sort=False):
        total = len(group)
        bucket_counts: dict[str, int] = {b: 0 for b in BUCKET_ORDER}
        for cat in group["OFFENSE_CATEGORY_ID"]:
            bucket_counts[bucket_for_category(cat)] += 1

        top_offenses = top_offense_types(group["OFFENSE_TYPE_ID"].tolist())

        year_counts: dict[int, int] = (
            group["year"].value_counts().sort_index().to_dict()
        )
        # Coerce numpy int keys to plain Python int for JSON serialization.
        year_counts = {int(k): int(v) for k, v in year_counts.items()}

        out[name] = {
            "total": total,
            "year_counts": year_counts,
            "year_range": (min(year_counts), max(year_counts)),
            "bucket_counts": bucket_counts,
            "top_offenses": top_offenses,
            "yoy_trend": compute_yoy_trend(year_counts),
        }
    return out


def build_documents(
    stats_per_neighborhood: dict[str, dict],
    centroids_by_name: dict[str, dict],
) -> list[Document]:
    """Build one Document per neighborhood with stats. Skips neighborhoods
    with zero matched incidents (not expected, but guards against weirdness)."""
    docs: list[Document] = []
    for name, stats in stats_per_neighborhood.items():
        if stats["total"] == 0:
            continue
        start_year, end_year = stats["year_range"]
        page_content = build_page_content(
            neighborhood_name=name,
            total=stats["total"],
            start_year=start_year,
            end_year=end_year,
            bucket_counts=stats["bucket_counts"],
            top_offenses=stats["top_offenses"],
            yoy_trend=stats["yoy_trend"],
            year_counts=stats["year_counts"],
        )
        centroid = centroids_by_name.get(name)
        full_metadata = {
            "neighborhood_name": name,
            "incident_count": stats["total"],
            "year_range": list(stats["year_range"]),
            "bucket_counts": stats["bucket_counts"],
            "top_offense_types": stats["top_offenses"],
            "yoy_trend": stats["yoy_trend"],
            "year_counts": stats["year_counts"],
        }
        metadata: dict = {
            "doc_type": DOC_TYPE,
            "neighborhood_name": name,
            "incident_count": stats["total"],
            "year_range": list(stats["year_range"]),
            "yoy_trend": stats["yoy_trend"],
            "service_name": SERVICE_NAME,
            "base_url": DATASET_HUB_URL,
            "hub_url": DATASET_HUB_URL,
            "map_url": DATASET_EXPLORE_URL,
            "has_layers": False,
            "full_metadata": json.dumps(full_metadata, default=str),
        }
        if centroid:
            metadata["location"] = centroid
        docs.append(Document(page_content=page_content, metadata=metadata))
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

    if not CRIME_CSV.exists():
        logger.error("crime CSV not found at %s", CRIME_CSV)
        return 1
    if not NEIGHBORHOOD_GEOJSON.exists():
        logger.error("neighborhoods geojson not found at %s", NEIGHBORHOOD_GEOJSON)
        return 1

    df = load_and_filter_crime(CRIME_CSV)

    names, polygons, centroids = load_neighborhood_polygons(NEIGHBORHOOD_GEOJSON)
    centroids_by_name = dict(zip(names, centroids))
    logger.info("loaded %d neighborhood polygons", len(names))

    logger.info("PIP-tagging %s incidents...", _format_int(len(df)))
    df["neighborhood_name"] = tag_neighborhoods(df, names, polygons)

    untagged = df["neighborhood_name"].isna().sum()
    if untagged:
        logger.info(
            "%s incidents fell outside any neighborhood polygon (dropping)",
            _format_int(int(untagged)),
        )
    df = df.dropna(subset=["neighborhood_name"])
    logger.info("tagged %s incidents across %d neighborhoods",
                _format_int(len(df)), df["neighborhood_name"].nunique())

    stats = aggregate_per_neighborhood(df)
    docs = build_documents(stats, centroids_by_name)
    logger.info("built %d documents", len(docs))

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
