import json
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
BASE_DIR = Path(__file__).resolve().parent.parent
GEOJSON_PATH = BASE_DIR / "data" / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://blue_cypher:blue_cypher@localhost:5432/blue_cypher_data")
TABLE_NAME = "neighborhood_demographics"

# Fields to drop — spatial/internal fields not useful for text responses
DROP_FIELDS = {"geometry", "OBJECTID", "GlobalID", "Shape__Area", "Shape__Length"}


def get_connection():
    return psycopg2.connect(POSTGRES_URL)


def create_table(cursor, columns: list[str]):
    """
    Dynamically create table based on the actual fields in the GeoJSON.
    NBHD_NAME and NBHD_ID are indexed for fast agent lookups.
    All numeric fields stored as NUMERIC to preserve decimal precision.
    """
    col_defs = []
    for col in columns:
        if col in ("NBHD_NAME",):
            col_defs.append(f'"{col}" TEXT')
        elif col in ("NBHD_ID", "DIST_NUM"):
            col_defs.append(f'"{col}" INTEGER')
        else:
            col_defs.append(f'"{col}" NUMERIC')

    cols_sql = ",\n    ".join(col_defs)
    create_sql = f"""
        DROP TABLE IF EXISTS {TABLE_NAME};
        CREATE TABLE {TABLE_NAME} (
            id SERIAL PRIMARY KEY,
            {cols_sql}
        );
        CREATE INDEX idx_{TABLE_NAME}_nbhd_name ON {TABLE_NAME} ("NBHD_NAME");
        CREATE INDEX idx_{TABLE_NAME}_nbhd_id ON {TABLE_NAME} ("NBHD_ID");
        CREATE INDEX idx_{TABLE_NAME}_dist_num ON {TABLE_NAME} ("DIST_NUM");
    """
    cursor.execute(create_sql)
    logger.info(f"Table '{TABLE_NAME}' created with {len(columns)} columns")


def load_geojson():
    with open(GEOJSON_PATH, "r") as f:
        data = json.load(f)

    features = data["features"]
    logger.info(f"Loaded {len(features)} features from GeoJSON")

    # Extract properties from first feature to determine columns
    # Filter out spatial/internal fields
    sample_props = features[0]["properties"]
    columns = [k for k in sample_props.keys() if k not in DROP_FIELDS]
    logger.info(f"Keeping {len(columns)} columns, dropping spatial fields")

    # Build rows
    rows = []
    for feature in features:
        props = feature["properties"]
        row = tuple(props.get(col) for col in columns)
        rows.append(row)

    return columns, rows


def main():
    columns, rows = load_geojson()

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Create table
                create_table(cur, columns)

                # Insert all rows
                col_names = ", ".join(f'"{c}"' for c in columns)
                insert_sql = f"INSERT INTO {TABLE_NAME} ({col_names}) VALUES %s"
                execute_values(cur, insert_sql, rows)

                logger.info(f"✅ Inserted {len(rows)} rows into '{TABLE_NAME}'")

                # Quick sanity check
                cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
                count = cur.fetchone()[0]
                logger.info(f"Row count confirmed: {count}")

                # Show a sample
                cur.execute(f'SELECT "NBHD_NAME", "Nmbr_Population", "Nmbr_PerCapitaIncome" FROM {TABLE_NAME} LIMIT 3')
                samples = cur.fetchall()
                logger.info("Sample rows:")
                for row in samples:
                    logger.info(f"  {row[0]}: population={row[1]}, per capita income=${row[2]}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()