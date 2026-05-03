"""ingest_denver_traffic.py

Read the Denver Open Data Catalog 5-year traffic accidents CSV and ingest one
Document per neighborhood into the existing denver_gis_catalog Qdrant
collection. Templated NL summaries — no LLM calls.

Source: Denver Open Data Catalog "Traffic Accidents (Offenses)" dataset
  https://opendata-geospatialdenver.hub.arcgis.com/datasets/db00bd99ea534d8987e0913a191ebe19_325/about
The CSV is gitignored (large); download manually into data/ before running.

Pipeline:
  1. Load CSV (~282k rows, despite the "5YR" filename the date range is
     actually 2013–present — explicit windowing required).
  2. Drop rows without geo_lat/geo_lon.
  3. Trim to the most recent 5 calendar years anchored to max(reported_date).
  4. Use the source neighborhood_id directly (already populated with proper-
     case ACS names — 78 distinct, 1:1 with the demographics geojson). Skip
     PIP entirely; just validate against the ACS name set and drop unmatched
     rows. ~4% loss expected for missing coords / unmatched names.
  5. Aggregate per neighborhood: total, 6-bucket category counts (from
     top_traffic_accident_offense), severity totals (sum of FATALITIES and
     SERIOUSLY_INJURED), vulnerable-road-user counts (bicycle_ind/
     pedestrian_ind), and yearly counts for YoY trend.
  6. Build templated summary, factual phrasing — no "dangerous" / "safe"
     verdicts. Mirrors the crime ingest's tone.

Run:
  python scripts/ingest_denver_traffic.py            # append (refuses if docs exist)
  python scripts/ingest_denver_traffic.py --purge    # delete prior docs first
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from shapely.geometry import shape

if TYPE_CHECKING:
    import pandas as pd

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TRAFFIC_CSV = DATA_DIR / "ODC_CRIME_TRAFFICACCIDENTS5YR_P_7255667086186507966.csv"
NEIGHBORHOOD_GEOJSON = DATA_DIR / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Traffic Accidents"
DOC_TYPE = "neighborhood_traffic_summary"
DATASET_HUB_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "db00bd99ea534d8987e0913a191ebe19_325/about"
)
DATASET_EXPLORE_URL = (
    "https://opendata-geospatialdenver.hub.arcgis.com/datasets/"
    "geospatialDenver::traffic-accidents-offenses/explore"
)

WINDOW_YEARS = 5
TREND_THRESHOLD = 0.10  # ±10% endpoint-to-endpoint = "up"/"down"; otherwise "flat"

# Maps the source top_traffic_accident_offense values (after stripping their
# trailing whitespace padding) to readable bucket labels. Bucket labels are
# used as both metadata keys and rendered text — single mapping serves both.
TRAFFIC_TYPE_BUCKETS: dict[str, str] = {
    "TRAF - ACCIDENT": "general",
    "TRAF - ACCIDENT - HIT & RUN": "hit-and-run",
    "TRAF - ACCIDENT - DUI/DUID": "DUI/DUID",
    "TRAF - ACCIDENT - SBI": "serious bodily injury",
    "TRAF - ACCIDENT - POLICE": "police-involved",
    "TRAF - ACCIDENT - FATAL": "fatal",
}
BUCKET_LABELS = list(TRAFFIC_TYPE_BUCKETS.values())


_unknown_offenses_seen: set[str] = set()


def bucket_for_offense(top_offense: str | None) -> str | None:
    """Map a stripped top_traffic_accident_offense value to a bucket label.
    Returns None for missing/unknown values (with a single warn log per
    novel value so we notice schema changes)."""
    if not top_offense:
        return None
    cleaned = top_offense.strip()
    if cleaned in TRAFFIC_TYPE_BUCKETS:
        return TRAFFIC_TYPE_BUCKETS[cleaned]
    if cleaned not in _unknown_offenses_seen:
        _unknown_offenses_seen.add(cleaned)
        logger.warning(
            "unknown top_traffic_accident_offense %r → not bucketed", cleaned,
        )
    return None


def compute_yoy_trend(year_counts: dict[int, int]) -> str:
    """Compare oldest vs newest year in the window. Same ±10% rule as crime.
    The newest year may be partial (data export cutoff mid-year); callers
    should mention the date range so the trend can be interpreted."""
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


def _format_int(n: int) -> str:
    return f"{n:,}"


def build_lead_sentence(
    *, neighborhood_name: str, total: int, start_year: int, end_year: int
) -> str:
    return (
        f"{neighborhood_name} reported {_format_int(total)} traffic accidents "
        f"from {start_year} through {end_year}."
    )


def build_type_sentence(bucket_counts: dict[str, int]) -> str | None:
    """Render bucket counts in descending order, omitting zero-count buckets.
    Returns None if all buckets are zero (shouldn't happen for non-empty
    aggregations, but defensive)."""
    ordered = sorted(
        ((label, bucket_counts.get(label, 0)) for label in BUCKET_LABELS),
        key=lambda kv: kv[1],
        reverse=True,
    )
    phrases = [
        f"{label} ({_format_int(count)})" for label, count in ordered if count > 0
    ]
    if not phrases:
        return None
    return "By type: " + ", ".join(phrases) + "."


def build_severity_sentence(fatalities: int, seriously_injured: int) -> str | None:
    """Surface human-impact totals — distinct from incident-categorization
    counts. Singular/plural-correct phrasing for low counts."""
    if fatalities <= 0 and seriously_injured <= 0:
        return None

    def _killed_phrase(n: int) -> str:
        return f"{_format_int(n)} {'person was' if n == 1 else 'people were'} killed"

    def _injured_phrase(n: int, with_subject: bool = True) -> str:
        verb = "was" if n == 1 else "were"
        if with_subject:
            subject = "person was" if n == 1 else "people were"
            return f"{_format_int(n)} {subject} seriously injured"
        return f"{_format_int(n)} {verb} seriously injured"

    if fatalities > 0 and seriously_injured > 0:
        return (
            f"Across these incidents, {_killed_phrase(fatalities)} and "
            f"{_injured_phrase(seriously_injured, with_subject=False)}."
        )
    if fatalities > 0:
        return f"Across these incidents, {_killed_phrase(fatalities)}."
    return f"Across these incidents, {_injured_phrase(seriously_injured)}."


def build_vru_sentence(pedestrian_count: int, bicycle_count: int) -> str | None:
    """Vulnerable road user counts — bicycle_ind / pedestrian_ind flags."""
    if pedestrian_count <= 0 and bicycle_count <= 0:
        return None
    if pedestrian_count > 0 and bicycle_count > 0:
        return (
            f"{_format_int(pedestrian_count)} accidents involved pedestrians "
            f"and {_format_int(bicycle_count)} involved bicyclists."
        )
    if pedestrian_count > 0:
        return f"{_format_int(pedestrian_count)} accidents involved pedestrians."
    return f"{_format_int(bicycle_count)} accidents involved bicyclists."


def build_trend_sentence(
    year_counts: dict[int, int], yoy_trend: str
) -> str | None:
    if len(year_counts) < 2:
        return None
    years_sorted = sorted(year_counts)
    oldest, newest = years_sorted[0], years_sorted[-1]
    trend_word = {
        "up": "increased",
        "down": "decreased",
        "flat": "stayed roughly flat",
    }[yoy_trend]
    return (
        f"Year-over-year, accident counts {trend_word} from "
        f"{_format_int(year_counts[oldest])} in {oldest} to "
        f"{_format_int(year_counts[newest])} in {newest}."
    )


def build_page_content(
    *,
    neighborhood_name: str,
    total: int,
    start_year: int,
    end_year: int,
    bucket_counts: dict[str, int],
    fatalities: int,
    seriously_injured: int,
    pedestrian_count: int,
    bicycle_count: int,
    year_counts: dict[int, int],
    yoy_trend: str,
) -> str:
    """Compose the embedded NL summary for one neighborhood."""
    sentences = [
        build_lead_sentence(
            neighborhood_name=neighborhood_name,
            total=total, start_year=start_year, end_year=end_year,
        ),
    ]
    for sentence in (
        build_type_sentence(bucket_counts),
        build_severity_sentence(fatalities, seriously_injured),
        build_vru_sentence(pedestrian_count, bicycle_count),
        build_trend_sentence(year_counts, yoy_trend),
    ):
        if sentence:
            sentences.append(sentence)
    return " ".join(sentences)


def load_neighborhood_centroids(geojson_path: Path) -> dict[str, dict]:
    """Returns {NBHD_NAME: {"lat": float, "lon": float}}. Same source of
    truth used by demographics and crime."""
    with geojson_path.open() as f:
        data = json.load(f)
    out: dict[str, dict] = {}
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
        # shapely on EPSG:4326: point.x = lon, point.y = lat (deliberately flipped).
        out[name] = {"lat": c.y, "lon": c.x}
    return out


def load_and_filter_traffic(csv_path: Path) -> "pd.DataFrame":
    """Load, drop missing coords, parse reported_date, trim to last
    WINDOW_YEARS calendar years anchored to the most recent record's year.
    Strips whitespace padding from neighborhood_id and top_traffic_accident_offense."""
    import pandas as pd

    logger.info("loading %s", csv_path.name)
    df = pd.read_csv(
        csv_path,
        encoding="utf-8-sig",  # the file has a BOM
        usecols=[
            "top_traffic_accident_offense",
            "reported_date",
            "geo_lon",
            "geo_lat",
            "neighborhood_id",
            "bicycle_ind",
            "pedestrian_ind",
            "SERIOUSLY_INJURED",
            "FATALITIES",
        ],
        dtype={
            "top_traffic_accident_offense": "string",
            "reported_date": "string",
            "neighborhood_id": "string",
        },
        low_memory=False,
    )
    initial = len(df)
    logger.info("loaded %s rows", _format_int(initial))

    df = df.dropna(subset=["geo_lat", "geo_lon"])
    logger.info("after dropping rows without coords: %s rows", _format_int(len(df)))

    df["reported_date"] = pd.to_datetime(
        df["reported_date"], format="%m/%d/%Y %I:%M:%S %p", errors="coerce"
    )
    df = df.dropna(subset=["reported_date"])

    # Strip whitespace padding from string fields (source has e.g.
    # "TRAF - ACCIDENT               " and " 2     ").
    df["top_traffic_accident_offense"] = (
        df["top_traffic_accident_offense"].fillna("").str.strip()
    )
    df["neighborhood_id"] = df["neighborhood_id"].fillna("").str.strip()

    df["year"] = df["reported_date"].dt.year.astype(int)
    max_year = int(df["year"].max())
    min_window_year = max_year - (WINDOW_YEARS - 1)
    df = df[df["year"] >= min_window_year]
    logger.info(
        "windowed to last %d years (%d–%d): %s rows",
        WINDOW_YEARS, min_window_year, max_year, _format_int(len(df)),
    )

    return df


def filter_to_known_neighborhoods(
    df: "pd.DataFrame", known_names: set[str]
) -> "pd.DataFrame":
    """Drop rows whose neighborhood_id is missing or doesn't match the ACS
    name set. Logs the drop count."""
    before = len(df)
    df = df[df["neighborhood_id"].isin(known_names)]
    dropped = before - len(df)
    if dropped:
        logger.info(
            "dropped %s rows with missing/unknown neighborhood_id",
            _format_int(dropped),
        )
    return df


def aggregate_per_neighborhood(df: "pd.DataFrame") -> dict[str, dict]:
    """Returns {neighborhood_name: stats_dict}. Each stats_dict has:
      total, year_counts, year_range, bucket_counts (5-bucket), fatalities
      (sum), seriously_injured (sum), pedestrian_count, bicycle_count,
      yoy_trend."""
    import pandas as pd

    out: dict[str, dict] = {}
    for name, group in df.groupby("neighborhood_id", sort=False):
        total = len(group)

        bucket_counts = {label: 0 for label in BUCKET_LABELS}
        for offense in group["top_traffic_accident_offense"]:
            bucket = bucket_for_offense(offense)
            if bucket is not None:
                bucket_counts[bucket] += 1

        # Numeric columns may have NaN; coerce + sum.
        fatalities = int(pd.to_numeric(group["FATALITIES"], errors="coerce").fillna(0).sum())
        seriously_injured = int(
            pd.to_numeric(group["SERIOUSLY_INJURED"], errors="coerce").fillna(0).sum()
        )
        pedestrian_count = int(
            pd.to_numeric(group["pedestrian_ind"], errors="coerce").fillna(0).gt(0).sum()
        )
        bicycle_count = int(
            pd.to_numeric(group["bicycle_ind"], errors="coerce").fillna(0).gt(0).sum()
        )

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
            "fatalities": fatalities,
            "seriously_injured": seriously_injured,
            "pedestrian_count": pedestrian_count,
            "bicycle_count": bicycle_count,
            "yoy_trend": compute_yoy_trend(year_counts),
        }
    return out


def build_documents(
    stats_per_neighborhood: dict[str, dict],
    centroids_by_name: dict[str, dict],
) -> list[Document]:
    """One Document per neighborhood with non-zero accident counts."""
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
            fatalities=stats["fatalities"],
            seriously_injured=stats["seriously_injured"],
            pedestrian_count=stats["pedestrian_count"],
            bicycle_count=stats["bicycle_count"],
            year_counts=stats["year_counts"],
            yoy_trend=stats["yoy_trend"],
        )
        centroid = centroids_by_name.get(name)
        full_metadata = {
            "neighborhood_name": name,
            "incident_count": stats["total"],
            "year_range": list(stats["year_range"]),
            "bucket_counts": stats["bucket_counts"],
            "fatalities": stats["fatalities"],
            "seriously_injured": stats["seriously_injured"],
            "pedestrian_count": stats["pedestrian_count"],
            "bicycle_count": stats["bicycle_count"],
            "yoy_trend": stats["yoy_trend"],
            "year_counts": stats["year_counts"],
        }
        metadata: dict = {
            "doc_type": DOC_TYPE,
            "neighborhood_name": name,
            "incident_count": stats["total"],
            "year_range": list(stats["year_range"]),
            "fatalities": stats["fatalities"],
            "seriously_injured": stats["seriously_injured"],
            "pedestrian_count": stats["pedestrian_count"],
            "bicycle_count": stats["bicycle_count"],
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

    if not TRAFFIC_CSV.exists():
        logger.error("traffic CSV not found at %s", TRAFFIC_CSV)
        return 1
    if not NEIGHBORHOOD_GEOJSON.exists():
        logger.error("neighborhoods geojson not found at %s", NEIGHBORHOOD_GEOJSON)
        return 1

    df = load_and_filter_traffic(TRAFFIC_CSV)

    centroids_by_name = load_neighborhood_centroids(NEIGHBORHOOD_GEOJSON)
    known_names = set(centroids_by_name)
    logger.info("loaded %d ACS neighborhood names + centroids", len(known_names))

    df = filter_to_known_neighborhoods(df, known_names)
    logger.info(
        "tagged %s incidents across %d neighborhoods",
        _format_int(len(df)), df["neighborhood_id"].nunique(),
    )

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
