"""ingest_neighborhoods.py

Reads data/neighborhood_summaries.json and upserts per-neighborhood, per-topic
section chunks into the existing denver_gis_catalog Qdrant collection. Uses the
same hybrid embedding setup as scripts/ingest.py.

Run: python scripts/ingest_neighborhoods.py
"""

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SUMMARIES_PATH = BASE_DIR / "data" / "neighborhood_summaries.json"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")

SERVICE_NAME = "Denver Neighborhood Demographics (ACS 2017-2021)"
BASE_URL = "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/ArcGIS/rest/services/ODC_POP_ACS20172021NBRHDCOMMON_A/FeatureServer"
HUB_URL = "https://opendata-geospatialdenver.hub.arcgis.com/datasets/0b1bbc1ff0ca4168b4434795245bc30f_362/about"

TOPIC_LABELS = {
    "population": "Population",
    "housing": "Housing",
    "education": "Education",
    "income_poverty": "Income & Poverty",
    "demographics": "Race & Ethnicity",
}


def build_documents(summaries: list[dict]) -> list[Document]:
    docs = []
    for entry in summaries:
        nbhd_name = entry["nbhd_name"]
        nbhd_id = entry.get("nbhd_id")
        dist_num = entry.get("dist_num")
        raw_properties = entry.get("raw_properties", {})

        for topic_key, section_text in entry.get("sections", {}).items():
            if not section_text:
                continue
            topic_label = TOPIC_LABELS.get(topic_key, topic_key)
            page_content = f"{nbhd_name} — {topic_label}: {section_text}"
            docs.append(
                Document(
                    page_content=page_content,
                    metadata={
                        "doc_type": "neighborhood_demographics",
                        "neighborhood_name": nbhd_name,
                        "neighborhood_id": nbhd_id,
                        "district_num": dist_num,
                        "topic": topic_key,
                        "service_name": SERVICE_NAME,
                        "base_url": BASE_URL,
                        "hub_url": HUB_URL,
                        "has_layers": False,
                        "full_metadata": json.dumps(raw_properties),
                    },
                )
            )
    return docs


def main() -> int:
    if not SUMMARIES_PATH.exists():
        logger.error(
            "summaries not found at %s — run generate_neighborhood_summaries.py first",
            SUMMARIES_PATH,
        )
        return 1

    with SUMMARIES_PATH.open() as f:
        summaries = json.load(f)

    logger.info("loaded %d neighborhood summary records", len(summaries))

    docs = build_documents(summaries)
    logger.info("built %d section documents", len(docs))

    dense = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    sparse = FastEmbedSparse(model_name="Qdrant/bm25")

    logger.info("connecting to Qdrant at %s, collection=%s", QDRANT_URL, COLLECTION_NAME)
    store = QdrantVectorStore.from_existing_collection(
        embedding=dense,
        sparse_embedding=sparse,
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
    )

    logger.info("adding %d documents to collection (existing entries preserved)", len(docs))
    store.add_documents(docs)
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
