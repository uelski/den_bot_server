import os
import logging
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = "denver_gis_catalog"

# The exact service_name value as it appears in your Qdrant metadata
SERVICE_NAMES = [
    "ODC_POP_ACS20172021NBRHDCOMMON_A"
    "ACS20192023NBHD_POPINDICATORS2023",
    "ACS20192023TRACT_POPINDICATORS2023",
    "Air_Quality_Index_Layers_WFL1",
    "CityWide_Demographics",
    "Common_Data_Indicators_American_Community_Survery_Neighborhoods_20192023",
    "ODC_POP_ACS20162020NBRHDCOMMON_A",
    "ODC_POP_ACS20182022NBRHDCOMMON_A",
    "ODC_POP_AMERCMTYSVY20162020NBRHD_A",
    "ODC_POP_AMERCMTYSVY20172021BLKGP_A",
    "ODC_POP_AMERCMTYSVY20172021NBRHD_A"
]

# Fields to add to the Qdrant record
DEMOGRAPHICS_TAG = {
    "pg_table": "neighborhood_demographics",
    "topic": "demographics",
    "is_current": True,
}


def main():
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    tagged = 0
    for SERVICE_NAME in SERVICE_NAMES:
        # Find the matching point by service_name
        results, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="metadata.service_name",
                        match=MatchValue(value=SERVICE_NAME),
                    )
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )

        if not results:
            logger.warning(f"No Qdrant point found for service_name='{SERVICE_NAME}'")
            logger.warning("Check that the service_name matches exactly — it is case sensitive")
            continue

        point = results[0]
        point_id = point.id
        existing_metadata = point.payload.get("metadata", {})

        logger.info(f"Found point id={point_id} for '{SERVICE_NAME}'")
        logger.info(f"Existing metadata keys: {list(existing_metadata.keys())}")

        # Merge new tags into existing metadata — preserves all existing fields
        updated_metadata = {**existing_metadata, **DEMOGRAPHICS_TAG}

        client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={"metadata": updated_metadata},
            points=[point_id],
        )

        logger.info(f"✅ Tagged '{SERVICE_NAME}' with:")
        tagged += 1
    
    logger.info(f"Done. Tagged {tagged}/{len(SERVICE_NAMES)} records.")


if __name__ == "__main__":
    main()