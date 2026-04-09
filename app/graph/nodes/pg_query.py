"""pg_query node — LLM-generated SQL against Postgres neighborhood demographics."""

import logging
import os
import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import get_engine
from app.graph.state import AgentState
from app.prompts.pg_query_prompt import PG_QUERY_HUMAN, PG_QUERY_SYSTEM

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_ROWS = 50

ALLOWED_COLUMNS = {
    "nbhd_name", "nbhd_id", "dist_num", "nmbr_population",
    "nmbr_percapitaincome", "pct_populationinpoverty", "pct_populationminority",
}

# Map lowercase to actual column names for quoting
COLUMN_NAME_MAP = {
    "nbhd_name": '"NBHD_NAME"',
    "nbhd_id": '"NBHD_ID"',
    "dist_num": '"DIST_NUM"',
    "nmbr_population": '"Nmbr_Population"',
    "nmbr_percapitaincome": '"Nmbr_PerCapitaIncome"',
    "pct_populationinpoverty": '"Pct_PopulationInPoverty"',
    "pct_populationminority": '"Pct_PopulationMinority"',
}

DANGEROUS_PATTERN = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|GRANT|TRUNCATE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


class SQLQueryOutput(BaseModel):
    where_clause: str = Field(description="SQL WHERE clause with :param_N placeholders, or empty string")
    params: dict[str, str] = Field(description="Mapping of placeholder names to values")
    order_by: str | None = Field(default=None, description="ORDER BY expression, e.g. 'Nmbr_Population DESC'")
    limit: int = Field(default=10, description="Number of rows to return, max 50")


def _validate_clause(clause: str) -> bool:
    """Reject clauses with dangerous keywords or disallowed columns."""
    if DANGEROUS_PATTERN.search(clause):
        logger.warning("pg_query: dangerous keyword detected in WHERE clause: %s", clause)
        return False

    # Extract column-like identifiers and check against allowlist
    identifiers = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", clause)
    sql_keywords = {"and", "or", "not", "is", "null", "in", "like", "ilike", "between", "true", "false"}
    for ident in identifiers:
        lower = ident.lower()
        if lower in sql_keywords or lower.startswith("param_"):
            continue
        if lower not in ALLOWED_COLUMNS:
            logger.warning("pg_query: disallowed identifier '%s' in WHERE clause", ident)
            return False
    return True


def _quote_columns(clause: str) -> str:
    """Replace unquoted column references with properly quoted versions."""
    for lower_name, quoted in COLUMN_NAME_MAP.items():
        # Match the column name case-insensitively as a whole word
        clause = re.sub(
            rf"\b{re.escape(lower_name)}\b",
            quoted,
            clause,
            flags=re.IGNORECASE,
        )
    return clause


def _validate_order_by(order_by: str) -> bool:
    """Validate ORDER BY only references allowed columns."""
    identifiers = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", order_by)
    order_keywords = {"asc", "desc", "nulls", "first", "last"}
    for ident in identifiers:
        lower = ident.lower()
        if lower in order_keywords:
            continue
        if lower not in ALLOWED_COLUMNS:
            logger.warning("pg_query: disallowed identifier '%s' in ORDER BY", ident)
            return False
    return True


async def pg_query(state: AgentState) -> dict[str, Any]:
    """Generate and execute a SQL query against the neighborhood demographics table."""
    table = state.get("pg_table")
    if not table:
        logger.warning("pg_query: no pg_table in state")
        return {"pg_query_results": None}

    # LLM generates the WHERE clause
    llm = ChatGoogleGenerativeAI(model=MODEL, temperature=0)
    structured_llm = llm.with_structured_output(SQLQueryOutput)

    prompt = ChatPromptTemplate.from_messages(
        [("system", PG_QUERY_SYSTEM), ("human", PG_QUERY_HUMAN)]
    )
    chain = prompt | structured_llm

    try:
        result: SQLQueryOutput = chain.invoke({"query": state["query"]})
    except Exception as exc:
        logger.warning("pg_query: LLM failed to generate SQL — %s", exc)
        return {"pg_query_results": None}

    # Build and validate the query
    limit = min(result.limit, MAX_ROWS)

    where = ""
    if result.where_clause.strip():
        if not _validate_clause(result.where_clause):
            return {"pg_query_results": None}
        where = f"WHERE {_quote_columns(result.where_clause)}"

    order = ""
    if result.order_by and result.order_by.strip():
        if not _validate_order_by(result.order_by):
            return {"pg_query_results": None}
        order = f"ORDER BY {_quote_columns(result.order_by)}"

    sql = f"SELECT * FROM {table} {where} {order} LIMIT :_limit"
    params = {**result.params, "_limit": limit}

    logger.info("pg_query: executing — %s | params=%s", sql, params)

    # Execute
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params)
            columns = list(rows.keys())
            results = [dict(zip(columns, row)) for row in rows.fetchall()]
    except Exception as exc:
        logger.warning("pg_query: query execution failed — %s", exc)
        return {"pg_query_results": None}

    logger.info("pg_query: returned %d rows", len(results))
    return {"pg_query_results": results}
