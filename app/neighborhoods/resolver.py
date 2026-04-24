"""resolver.py — resolve user phrasing to canonical Denver neighborhood names.

Three-tier cascade:
    1. Exact alias lookup (ALIASES dict, case-insensitive key).
    2. Exact official-name match (case-insensitive).
    3. LLM resolution grounded in OFFICIAL_NAMES + ALIASES as few-shot examples.

Tiers 1 and 2 are ~zero-cost hash lookups. Tier 3 calls Gemini with structured
output constrained to one of the 78 official names or null.

Results are memoized by normalized query string.
"""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from app.prompts.neighborhood_resolver_prompt import (
    RESOLVER_HUMAN,
    RESOLVER_SYSTEM,
)

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GEOJSON_PATH = BASE_DIR / "data" / "ODC_POP_ACS20172021NBRHDCOMMON.geojson"


def _load_official_names() -> list[str]:
    """Load the 78 Denver neighborhood names from the source geojson."""
    if not GEOJSON_PATH.exists():
        logger.warning("geojson not found at %s; OFFICIAL_NAMES will be empty", GEOJSON_PATH)
        return []
    with GEOJSON_PATH.open() as f:
        data = json.load(f)
    names = sorted(
        {
            (feature.get("properties") or {}).get("NBHD_NAME")
            for feature in data.get("features", [])
            if (feature.get("properties") or {}).get("NBHD_NAME")
        }
    )
    return names


OFFICIAL_NAMES: list[str] = _load_official_names()
_OFFICIAL_NAMES_LOWER: dict[str, str] = {n.lower(): n for n in OFFICIAL_NAMES}


# Static alias map: lowercase user phrase -> official name. Grow organically as
# new slang surfaces in real queries.
ALIASES: dict[str, str] = {
    "rino": "Five Points",
    "river north": "Five Points",
    "wash park": "Washington Park",
    "washpark": "Washington Park",
    "cap hill": "Capitol Hill",
    "caphill": "Capitol Hill",
    "lodo": "Union Station",
    "lower downtown": "Union Station",
}


def _validate_aliases() -> None:
    """Warn if any alias maps to a name not in OFFICIAL_NAMES."""
    unknown = [v for v in ALIASES.values() if v not in OFFICIAL_NAMES]
    if unknown and OFFICIAL_NAMES:
        logger.warning(
            "ALIASES contains %d values not present in OFFICIAL_NAMES: %s",
            len(unknown),
            unknown,
        )


_validate_aliases()


class ResolvedNeighborhood(BaseModel):
    """Result of resolving a user phrase to an official neighborhood.

    `name` is None when no confident match was found. Callers should treat None
    the same way they'd treat an unresolvable location (prompt the user for
    clarification, or fail gracefully).
    """

    name: str | None = Field(
        description="Exactly one of the official Denver neighborhood names, or null if no match.",
        default=None,
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Resolver confidence in the result.",
        default="low",
    )
    reasoning: str = Field(
        description="One-sentence explanation of why this name was chosen (for debugging).",
        default="",
    )


def _normalize(query: str) -> str:
    return query.strip().lower()


def _static_resolve(query: str) -> ResolvedNeighborhood | None:
    """Tier 1 + Tier 2: alias map and exact official-name match, both case-insensitive.

    Returns None if neither tier matches (caller falls through to the LLM).
    """
    norm = _normalize(query)
    if not norm:
        return None

    if norm in ALIASES:
        return ResolvedNeighborhood(
            name=ALIASES[norm],
            confidence="high",
            reasoning=f"Matched known alias '{query.strip()}' -> '{ALIASES[norm]}'.",
        )

    if norm in _OFFICIAL_NAMES_LOWER:
        return ResolvedNeighborhood(
            name=_OFFICIAL_NAMES_LOWER[norm],
            confidence="high",
            reasoning="Exact match against official neighborhood name.",
        )

    return None


def _format_aliases_for_prompt() -> str:
    return "\n".join(f'  "{k}" -> {v}' for k, v in sorted(ALIASES.items()))


def _format_official_names_for_prompt() -> str:
    return "\n".join(f"  - {n}" for n in OFFICIAL_NAMES)


def _build_chain():
    """Build the Gemini structured-output chain. Kept as a function so tests can
    patch the LLM class and each call uses a fresh instance."""
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(ResolvedNeighborhood)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", RESOLVER_SYSTEM),
            ("human", RESOLVER_HUMAN),
        ]
    ).partial(
        official_names=_format_official_names_for_prompt(),
        alias_examples=_format_aliases_for_prompt(),
    )
    return prompt | structured_llm


def _llm_resolve(query: str) -> ResolvedNeighborhood:
    """Tier 3: call Gemini with structured output constrained to OFFICIAL_NAMES or null."""
    try:
        chain = _build_chain()
        result: ResolvedNeighborhood = chain.invoke({"query": query})
    except Exception as exc:
        logger.warning("LLM neighborhood resolution failed for %r: %s", query, exc)
        return ResolvedNeighborhood(
            name=None,
            confidence="low",
            reasoning=f"LLM call failed: {exc}",
        )

    # Defensive post-check: reject names not in OFFICIAL_NAMES (LLM occasionally
    # hallucinates a close-but-wrong name).
    if result.name is not None and result.name not in OFFICIAL_NAMES:
        logger.warning(
            "LLM returned invalid neighborhood %r for query %r; coercing to null",
            result.name,
            query,
        )
        return ResolvedNeighborhood(
            name=None,
            confidence="low",
            reasoning=f"LLM returned non-official name '{result.name}'.",
        )

    return result


@lru_cache(maxsize=512)
def _resolve_cached(normalized_query: str) -> ResolvedNeighborhood:
    """Cache layer keyed on normalized query. Only tier 3 is genuinely expensive,
    but caching tiers 1 and 2 is essentially free and keeps the API uniform."""
    static = _static_resolve(normalized_query)
    if static is not None:
        return static
    return _llm_resolve(normalized_query)


def resolve(query: str) -> ResolvedNeighborhood:
    """Resolve a user phrase to a canonical Denver neighborhood name.

    Always returns a ResolvedNeighborhood. `name` is None when no confident
    match was found; callers should handle that case explicitly.
    """
    if not query or not query.strip():
        return ResolvedNeighborhood(
            name=None,
            confidence="low",
            reasoning="Empty query.",
        )
    return _resolve_cached(_normalize(query))


def clear_cache() -> None:
    """Clear the resolver memoization. Useful for tests."""
    _resolve_cached.cache_clear()
