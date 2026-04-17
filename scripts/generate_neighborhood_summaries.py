"""generate_neighborhood_summaries.py

Reads data/ODC_POP_ACS20172021NBRHDCOMMON.geojson and, per neighborhood, calls
Gemini to produce five topic-scoped natural-language summary sections. Writes
the result to data/neighborhood_summaries.json.

Run: python scripts/generate_neighborhood_summaries.py
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

BASE_DIR = Path(__file__).resolve().parent.parent
GEOJSON_PATH = BASE_DIR / "data" / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"
OUTPUT_PATH = BASE_DIR / "data" / "neighborhood_summaries.json"

DROP_FIELDS = {"OBJECTID", "GlobalID", "Shape__Area", "Shape__Length"}
IDENTITY_FIELDS = {"NBHD_NAME", "NBHD_ID", "DIST_NUM"}

SECTION_FIELDS = {
    "population": [
        "Nmbr_Population",
        "Nmbr_PopulationDensity",
        "Nmbr_PopCivilianNonInstitution",
        "Nmbr_PopulationUnder18",
        "Pct_PopulationUnder18",
        "Nmbr_PopulationOver70",
        "Pct_PopulationOver70",
        "Nmbr_PopulationWithDisability",
        "Pct_PopulationWithDisability",
        "Nmbr_PopUnder18InHouseholds",
        "Nmbr_Under18SingleParentFam",
        "Pct_Under18InSingleParentFam",
    ],
    "housing": [
        "Nmbr_Households",
        "Nmbr_OccupiedUnits",
        "Nmbr_OccupiedUnitsWithOwners",
        "Pct_OccupiedUnitsWithOwners",
        "Nmbr_OccupiedUnitsWithRenters",
        "Pct_OccupiedUnitsWithRenters",
        "Nmbr_HouseholdsCostBurdened",
        "Pct_HouseholdsCostBurdened",
        "Nmbr_HouseholdsNoVehAccess",
        "Pct_HouseholdsNoVehAccess",
    ],
    "education": [
        "Nmbr_Age25Plus",
        "Pct_Age25Plus",
        "Nmbr_Age25Plus_NoDiplomaOrGED",
        "Pct_Age25Plus_NoDiplomaOrGED",
        "Nmbr_Age25Plus_NoCollDegree",
        "Pct_Age25Plus_NoCollDegree",
        "Nmbr_Age25Plus_LessThanBA",
        "Pct_Age25Plus_LessThanBA",
    ],
    "income_poverty": [
        "Nmbr_PerCapitaIncome",
        "Nmbr_PopulationPovDetermined",
        "Nmbr_PopulationInPoverty",
        "Pct_PopulationInPoverty",
        "Pct_PopOver16Unemployed",
    ],
    "demographics": [
        "Nmbr_PopNonHispanicWhite",
        "Pct_PopNonHispanicWhite",
        "Nmbr_PopulationMinority",
        "Pct_PopulationMinority",
    ],
}


class NeighborhoodSummary(BaseModel):
    population: str = Field(description="2-4 sentences about population size, density, age distribution, disability, family/household composition. Must mention the neighborhood name. Use concrete numbers.")
    housing: str = Field(description="2-4 sentences about households, owner/renter mix, cost burden, and no-vehicle households. Must mention the neighborhood name. Use concrete numbers.")
    education: str = Field(description="2-4 sentences about educational attainment among adults 25 and older. Must mention the neighborhood name. Use concrete numbers.")
    income_poverty: str = Field(description="2-4 sentences about per-capita income, poverty, and unemployment. Must mention the neighborhood name. Use concrete numbers.")
    demographics: str = Field(description="2-4 sentences about race/ethnicity — non-Hispanic white share and minority share. Must mention the neighborhood name. Use concrete numbers.")


SYSTEM_PROMPT = """You are a data analyst writing neighborhood-level demographic summaries for Denver, Colorado.

You will receive ACS 2017-2021 metrics for a single Denver neighborhood, organized into five topical groups.
Write one natural-language summary per topic. Each summary must:
- Be 2-4 sentences
- Include the neighborhood name prominently (every section)
- Cite concrete numbers from the metrics (counts and percentages)
- Be factual; do not speculate or infer causation
- Round percentages to one decimal and counts to the nearest integer when helpful"""


def _format_metrics(properties: dict, field_names: list[str]) -> str:
    lines = []
    for name in field_names:
        value = properties.get(name)
        if value is None:
            continue
        lines.append(f"- {name}: {value}")
    return "\n".join(lines) if lines else "(no metrics available)"


def _build_human_prompt(properties: dict) -> str:
    nbhd_name = properties.get("NBHD_NAME", "Unknown")
    dist_num = properties.get("DIST_NUM", "?")
    nbhd_id = properties.get("NBHD_ID", "?")

    section_blocks = []
    for section, fields in SECTION_FIELDS.items():
        block = f"### {section}\n{_format_metrics(properties, fields)}"
        section_blocks.append(block)

    return (
        f"Neighborhood: {nbhd_name} (Neighborhood ID {nbhd_id}, Council District {dist_num})\n\n"
        + "\n\n".join(section_blocks)
        + "\n\nWrite one summary per section."
    )


def summarize_neighborhood(chain, properties: dict) -> NeighborhoodSummary:
    human = _build_human_prompt(properties)
    return chain.invoke({"input": human})


def main() -> int:
    if not GEOJSON_PATH.exists():
        logger.error("geojson not found at %s", GEOJSON_PATH)
        return 1

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        logger.error("set GEMINI_API_KEY or GOOGLE_API_KEY in .env")
        return 1

    with GEOJSON_PATH.open() as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    logger.info("loaded %d features", len(features))

    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0.2)
    structured = llm.with_structured_output(NeighborhoodSummary)
    prompt = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("human", "{input}")]
    )
    chain = prompt | structured

    out = []
    for idx, feature in enumerate(features, start=1):
        props = feature.get("properties", {}) or {}
        raw_properties = {
            k: v for k, v in props.items() if k not in DROP_FIELDS
        }

        nbhd_name = raw_properties.get("NBHD_NAME")
        if not nbhd_name:
            logger.warning("feature %d missing NBHD_NAME, skipping", idx)
            continue

        logger.info("[%d/%d] summarizing %s", idx, len(features), nbhd_name)

        try:
            summary = summarize_neighborhood(chain, raw_properties)
        except Exception as exc:
            logger.warning("failed for %s — %s", nbhd_name, exc)
            continue

        out.append({
            "nbhd_name": nbhd_name,
            "nbhd_id": raw_properties.get("NBHD_ID"),
            "dist_num": raw_properties.get("DIST_NUM"),
            "sections": summary.model_dump(),
            "raw_properties": raw_properties,
        })

        time.sleep(0.2)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as f:
        json.dump(out, f, indent=2)

    logger.info("wrote %d neighborhood summaries to %s", len(out), OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
