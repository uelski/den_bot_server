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

# Human-readable labels fed to the LLM instead of raw column names.
FIELD_LABELS = {
    # Population
    "Nmbr_Population": "Total population",
    "Nmbr_PopulationDensity": "Population density",
    "Nmbr_PopCivilianNonInstitution": "Civilian non-institutionalized population",
    "Nmbr_PopulationUnder18": "Population under age 18",
    "Pct_PopulationUnder18": "Percentage of population under age 18",
    "Nmbr_PopulationOver70": "Population over age 70",
    "Pct_PopulationOver70": "Percentage of population over age 70",
    "Nmbr_PopulationWithDisability": "Population with a disability",
    "Pct_PopulationWithDisability": "Percentage of population with a disability",
    "Nmbr_PopUnder18InHouseholds": "Population under 18 living in households",
    "Nmbr_Under18SingleParentFam": "Population under 18 in single-parent families",
    "Pct_Under18InSingleParentFam": "Percentage of under 18 population in single-parent families",
    # Housing
    "Nmbr_Households": "Total households",
    "Nmbr_OccupiedUnits": "Occupied housing units",
    "Nmbr_OccupiedUnitsWithOwners": "Owner-occupied housing units",
    "Pct_OccupiedUnitsWithOwners": "Percentage of occupied units that are owner-occupied",
    "Nmbr_OccupiedUnitsWithRenters": "Renter-occupied housing units",
    "Pct_OccupiedUnitsWithRenters": "Percentage of occupied units that are renter-occupied",
    "Nmbr_HouseholdsCostBurdened": "Cost-burdened households (housing costs over 30% of income)",
    "Pct_HouseholdsCostBurdened": "Percentage of households that are cost-burdened",
    "Nmbr_HouseholdsNoVehAccess": "Households with no vehicle access",
    "Pct_HouseholdsNoVehAccess": "Percentage of households with no vehicle access",
    # Education
    "Nmbr_Age25Plus": "Population age 25 and older",
    "Pct_Age25Plus": "Percentage of population age 25 and older",
    "Nmbr_Age25Plus_NoDiplomaOrGED": "Population 25+ with no high school diploma or GED",
    "Pct_Age25Plus_NoDiplomaOrGED": "Percentage of 25+ population with no high school diploma or GED",
    "Nmbr_Age25Plus_NoCollDegree": "Population 25+ with no college degree",
    "Pct_Age25Plus_NoCollDegree": "Percentage of 25+ population with no college degree",
    "Nmbr_Age25Plus_LessThanBA": "Population 25+ with less than a bachelor's degree",
    "Pct_Age25Plus_LessThanBA": "Percentage of 25+ population with less than a bachelor's degree",
    # Income & poverty
    "Nmbr_PerCapitaIncome": "Per capita income (USD)",
    "Nmbr_PopulationPovDetermined": "Population for whom poverty status was determined",
    "Nmbr_PopulationInPoverty": "Population living below the poverty line",
    "Pct_PopulationInPoverty": "Percentage of population living below the poverty line",
    "Pct_PopOver16Unemployed": "Percentage of population over age 16 that is unemployed",
    # Race & ethnicity
    "Nmbr_PopNonHispanicWhite": "Non-Hispanic white population",
    "Pct_PopNonHispanicWhite": "Percentage of population that is non-Hispanic white",
    "Nmbr_PopulationMinority": "Minority (non-white or Hispanic) population",
    "Pct_PopulationMinority": "Percentage of population that is a racial or ethnic minority",
}


def _format_value(name: str, value) -> str:
    if name.startswith("Pct_"):
        return f"{value}%"
    if name == "Nmbr_PerCapitaIncome":
        return f"${value}"
    return str(value)


class NeighborhoodSummary(BaseModel):
    population: str = Field(description="Up to ~150 words about population: size, density, age distribution, disability, and family/household composition. Must mention the neighborhood name. Include every piece of related information — mention every metric provided. Use concrete numbers.")
    housing: str = Field(description="Up to ~150 words about households, owner/renter mix, cost burden, and no-vehicle access. Must mention the neighborhood name. Include every piece of related information — mention every metric provided. Use concrete numbers.")
    education: str = Field(description="Up to ~150 words about educational attainment among adults 25 and older. Must mention the neighborhood name. Include every piece of related information — mention every metric provided. Use concrete numbers.")
    income_poverty: str = Field(description="Up to ~150 words about per-capita income, poverty, and unemployment. Must mention the neighborhood name. Include every piece of related information — mention every metric provided. Use concrete numbers.")
    demographics: str = Field(description="Up to ~150 words about race/ethnicity — non-Hispanic white share and minority share. Must mention the neighborhood name. Include every piece of related information — mention every metric provided. Use concrete numbers.")


SYSTEM_PROMPT = """You are a data analyst writing neighborhood-level demographic summaries for Denver, Colorado.

You will receive ACS 2017-2021 metrics for a single Denver neighborhood, organized into five topical groups.
Write one natural-language summary per topic. Each summary must:
- Be up to ~150 words (enough to cover every metric comfortably)
- Include the neighborhood name prominently in every section
- **Mention every metric provided for that section.** Do not skip any listed metric — if you cannot fit them all naturally, add one more sentence. Coverage is mandatory.
- Cite concrete numbers from the metrics (both counts and percentages)
- Be factual; do not speculate or infer causation
- Round percentages to one decimal and counts to the nearest integer when helpful"""


def _format_metrics(properties: dict, field_names: list[str]) -> str:
    lines = []
    for name in field_names:
        value = properties.get(name)
        if value is None:
            continue
        label = FIELD_LABELS.get(name, name)
        lines.append(f"- {label}: {_format_value(name, value)}")
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


def _augment_with_fact_list(sections: dict, properties: dict) -> dict:
    """Append a complete fact list to each section so every metric is
    guaranteed to appear in the embedded text, even if the LLM omitted some."""
    augmented = {}
    for section, fields in SECTION_FIELDS.items():
        prose = sections.get(section, "")
        fact_lines = []
        for field in fields:
            value = properties.get(field)
            if value is None:
                continue
            label = FIELD_LABELS.get(field, field)
            fact_lines.append(f"- {label}: {_format_value(field, value)}")
        if fact_lines:
            augmented[section] = (
                f"{prose}\n\nKey metrics:\n" + "\n".join(fact_lines)
            ).strip()
        else:
            augmented[section] = prose
    return augmented


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

        sections = _augment_with_fact_list(summary.model_dump(), raw_properties)

        out.append({
            "nbhd_name": nbhd_name,
            "nbhd_id": raw_properties.get("NBHD_ID"),
            "dist_num": raw_properties.get("DIST_NUM"),
            "sections": sections,
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
