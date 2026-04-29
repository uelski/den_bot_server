"""ingest_rtd_gtfs.py

Read RTD's static GTFS feed (extracted into data/rtd_gtfs/) and ingest one
Document per stop and one per route into the existing denver_gis_catalog Qdrant
collection. Uses templated NL summaries — no LLM calls.

Layout assumed:
  data/rtd_gtfs/stops.txt
  data/rtd_gtfs/routes.txt
  data/rtd_gtfs/trips.txt
  data/rtd_gtfs/stop_times.txt
  data/rtd_gtfs/agency.txt

Run:
  python scripts/ingest_rtd_gtfs.py            # append (refuses if rtd docs already exist)
  python scripts/ingest_rtd_gtfs.py --purge    # delete prior rtd_stop / rtd_route docs first
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
from qdrant_client.models import FieldCondition, Filter, MatchAny

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
GTFS_DIR = BASE_DIR / "data" / "rtd_gtfs"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "RTD Transit (GTFS)"
STOP_URL_TEMPLATE = "https://app.rtd-denver.com/nextride/stop/{stop_id}"
ROUTE_URL_TEMPLATE = "https://app.rtd-denver.com/nextride/route/{route_id}"

STOP_DOC_TYPE = "rtd_stop"
ROUTE_DOC_TYPE = "rtd_route"

# GTFS route_type enum -> human-readable label for templated text.
ROUTE_TYPE_LABELS: dict[int, str] = {
    0: "Light Rail",
    1: "Subway",
    2: "Commuter Rail",
    3: "Bus",
    4: "Ferry",
    5: "Cable Tram",
    6: "Aerial Lift",
    7: "Funicular",
    11: "Trolleybus",
    12: "Monorail",
}


def _wheelchair_note(value) -> str:
    """Map GTFS wheelchair_boarding (0/1/2 or NaN) to a sentence-friendly suffix."""
    try:
        ival = int(value)
    except (TypeError, ValueError):
        return ""
    if ival == 1:
        return "Wheelchair accessible."
    if ival == 2:
        return "Not wheelchair accessible."
    return ""


def load_gtfs(data_dir: Path):
    """Read the GTFS files we need into pandas DataFrames.

    Returns (stops_df, routes_df, trips_df, stop_times_df, agency_name).
    Imports pandas locally so the script-only dependency doesn't pollute
    the running app's import graph.
    """
    import pandas as pd

    stops_df = pd.read_csv(data_dir / "stops.txt", dtype=str, low_memory=False)
    # Stops have lat/lon as strings in GTFS — convert.
    stops_df["stop_lat"] = stops_df["stop_lat"].astype(float)
    stops_df["stop_lon"] = stops_df["stop_lon"].astype(float)

    routes_df = pd.read_csv(data_dir / "routes.txt", dtype=str, low_memory=False)
    routes_df["route_type"] = routes_df["route_type"].astype(int)

    trips_df = pd.read_csv(
        data_dir / "trips.txt", usecols=["trip_id", "route_id"], dtype=str
    )
    stop_times_df = pd.read_csv(
        data_dir / "stop_times.txt",
        usecols=["trip_id", "stop_id"],
        dtype=str,
    )

    agency_name = "Regional Transportation District"
    try:
        agency_df = pd.read_csv(data_dir / "agency.txt", dtype=str)
        if not agency_df.empty:
            agency_name = str(agency_df.iloc[0].get("agency_name", agency_name))
    except FileNotFoundError:
        logger.warning("agency.txt not found — using default agency name")

    return stops_df, routes_df, trips_df, stop_times_df, agency_name


def build_stop_route_index(stop_times_df, trips_df) -> dict[str, list[str]]:
    """Join stop_times -> trips on trip_id, dedupe, group by stop_id.

    Returns {stop_id: [route_id, ...]}. Order within each list is not
    significant; we sort it for deterministic output.
    """
    pairs = stop_times_df.merge(trips_df, on="trip_id").drop_duplicates(
        ["stop_id", "route_id"]
    )
    index: dict[str, list[str]] = {}
    for stop_id, route_id in zip(pairs["stop_id"], pairs["route_id"]):
        index.setdefault(stop_id, []).append(route_id)
    for stop_id in index:
        index[stop_id] = sorted(index[stop_id])
    return index


def build_stop_documents(
    stops_df, stop_route_index, routes_df
) -> list[Document]:
    """Build one Document per stop. Skip stops with location_type != 0 (stations,
    entrances, etc. — not useful as a "where to catch a bus" answer). Skip
    stops with no routes (typically parent_station rows that join to nothing)."""
    # route_id -> route_short_name for templating
    short_name_by_route: dict[str, str] = dict(
        zip(routes_df["route_id"], routes_df["route_short_name"].fillna(""))
    )

    docs: list[Document] = []
    for row in stops_df.itertuples():
        stop_id = row.stop_id
        location_type = getattr(row, "location_type", "0") or "0"
        if str(location_type) not in ("", "0"):
            continue

        route_ids = stop_route_index.get(stop_id, [])
        if not route_ids:
            continue

        stop_name = (getattr(row, "stop_name", "") or "").strip()
        stop_code = (getattr(row, "stop_code", "") or "").strip() or stop_id
        lat = float(row.stop_lat)
        lon = float(row.stop_lon)
        wheelchair = getattr(row, "wheelchair_boarding", None)

        route_short_names = sorted({short_name_by_route.get(rid, rid) for rid in route_ids})
        route_label = ", ".join(route_short_names)
        wheelchair_text = _wheelchair_note(wheelchair)

        page_content = (
            f"{stop_name} (stop code {stop_code}) is an RTD transit stop in Denver "
            f"served by {len(route_ids)} route(s): {route_label}. "
            f"Located at {lat:.5f}, {lon:.5f}."
        )
        if wheelchair_text:
            page_content += f" {wheelchair_text}"

        stop_url = STOP_URL_TEMPLATE.format(stop_id=stop_id)
        full_metadata = {
            "stop_id": stop_id,
            "stop_code": stop_code,
            "stop_name": stop_name,
            "stop_lat": lat,
            "stop_lon": lon,
            "wheelchair_boarding": wheelchair,
            "route_ids": route_ids,
            "route_short_names": route_short_names,
        }

        docs.append(
            Document(
                page_content=page_content,
                metadata={
                    "doc_type": STOP_DOC_TYPE,
                    "stop_id": stop_id,
                    "stop_code": stop_code,
                    "stop_name": stop_name,
                    "location": {"lat": lat, "lon": lon},
                    "route_ids": route_ids,
                    "route_short_names": route_short_names,
                    "route_count": len(route_ids),
                    "wheelchair_boarding": wheelchair,
                    "service_name": SERVICE_NAME,
                    "base_url": stop_url,
                    "hub_url": stop_url,
                    "has_layers": False,
                    "full_metadata": json.dumps(full_metadata, default=str),
                },
            )
        )
    return docs


def build_route_documents(
    routes_df, stop_route_index, agency_name: str
) -> list[Document]:
    """Build one Document per route. stop_count comes from the inverted index."""
    # invert stop_route_index: route_id -> count of distinct stops
    stop_count_by_route: dict[str, int] = {}
    for stop_id, route_ids in stop_route_index.items():
        for rid in route_ids:
            stop_count_by_route[rid] = stop_count_by_route.get(rid, 0) + 1

    docs: list[Document] = []
    for row in routes_df.itertuples():
        route_id = row.route_id
        route_short_name = (getattr(row, "route_short_name", "") or "").strip()
        route_long_name = (getattr(row, "route_long_name", "") or "").strip()
        route_type = int(row.route_type)
        route_color = (getattr(row, "route_color", "") or "").strip() or None
        type_label = ROUTE_TYPE_LABELS.get(route_type, "Transit")
        stop_count = stop_count_by_route.get(route_id, 0)

        display_name = (route_long_name or route_short_name).strip()
        page_content = (
            f"The {route_short_name} {display_name} is an RTD {type_label} route "
            f"operated by {agency_name}. It serves {stop_count} stops across the "
            f"Denver metro area."
        ).replace("  ", " ")

        route_url = ROUTE_URL_TEMPLATE.format(route_id=route_id)
        full_metadata = {
            "route_id": route_id,
            "route_short_name": route_short_name,
            "route_long_name": route_long_name,
            "route_type": route_type,
            "route_type_label": type_label,
            "route_color": route_color,
            "agency_name": agency_name,
        }

        docs.append(
            Document(
                page_content=page_content,
                metadata={
                    "doc_type": ROUTE_DOC_TYPE,
                    "route_id": route_id,
                    "route_short_name": route_short_name,
                    "route_long_name": route_long_name,
                    "route_type": route_type,
                    "route_type_label": type_label,
                    "route_color": route_color,
                    "stop_count": stop_count,
                    "service_name": SERVICE_NAME,
                    "base_url": route_url,
                    "hub_url": route_url,
                    "has_layers": False,
                    "full_metadata": json.dumps(full_metadata, default=str),
                },
            )
        )
    return docs


def _count_existing_rtd_points(client: QdrantClient) -> int:
    flt = Filter(
        must=[
            FieldCondition(
                key="metadata.doc_type",
                match=MatchAny(any=[STOP_DOC_TYPE, ROUTE_DOC_TYPE]),
            )
        ]
    )
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


def _purge_existing_rtd_points(client: QdrantClient) -> None:
    flt = Filter(
        must=[
            FieldCondition(
                key="metadata.doc_type",
                match=MatchAny(any=[STOP_DOC_TYPE, ROUTE_DOC_TYPE]),
            )
        ]
    )
    client.delete(collection_name=COLLECTION_NAME, points_selector=flt)
    logger.info("purged existing rtd_stop / rtd_route points from %s", COLLECTION_NAME)


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
        help="Delete existing rtd_stop / rtd_route Qdrant points before insert.",
    )
    args = parser.parse_args()

    if not GTFS_DIR.exists():
        logger.error(
            "GTFS directory not found at %s — extract google_transit.zip there first",
            GTFS_DIR,
        )
        return 1

    stops_df, routes_df, trips_df, stop_times_df, agency_name = load_gtfs(GTFS_DIR)
    logger.info(
        "loaded GTFS: %d stops, %d routes, %d trips, %d stop_times rows",
        len(stops_df), len(routes_df), len(trips_df), len(stop_times_df),
    )

    stop_route_index = build_stop_route_index(stop_times_df, trips_df)
    logger.info("computed stop -> routes index: %d stops covered", len(stop_route_index))

    stop_docs = build_stop_documents(stops_df, stop_route_index, routes_df)
    route_docs = build_route_documents(routes_df, stop_route_index, agency_name)
    logger.info("built %d stop docs, %d route docs", len(stop_docs), len(route_docs))

    docs = stop_docs + route_docs

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    existing = _count_existing_rtd_points(client)
    if existing > 0:
        if args.purge:
            _purge_existing_rtd_points(client)
        else:
            logger.error(
                "found %d existing rtd_stop/rtd_route points in %s — re-run with --purge to replace, "
                "or delete them manually first",
                existing, COLLECTION_NAME,
            )
            return 1

    upsert_to_qdrant(docs)
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
