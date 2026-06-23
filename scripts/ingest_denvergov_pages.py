"""Ingest curated denvergov.org informational pages into the knowledge base.

These are pages the Tavily `search_denver_gov` tool doesn't reliably surface
(the saved list lives in ITERATION_V2.md § "Source URLs to ingest"). We fetch
each, extract the main content with trafilatura, and reuse the worker's
chunk → embed → upsert pipeline to write them into the SAME Qdrant KB
collection the PDFs use — so retrieval (catalog + KB fan-out) and the Cohere
reranker pick them up with zero graph changes.

Pages differ from uploaded PDFs in two ways, both carried in the payload:
  - `doc_type="denvergov_page"`  → downstream cites by URL ("View on
    denvergov.org"), NOT a GCS download (there's no uploaded file).
  - `document_id = <url>` (stable) → re-running this script OVERWRITES the
    page's chunks in place (unlike PDFs, whose document_id is timestamped).
    Stable IDs make a scheduled re-fetch idempotent.

DRY-RUN BY DEFAULT — fetch + extract + report per-URL text quality and chunk
count, with NO Qdrant writes. Eyeball the output, then pass --write to ingest.

    python scripts/ingest_denvergov_pages.py            # dry-run (safe)
    python scripts/ingest_denvergov_pages.py --write    # actually upsert
    python scripts/ingest_denvergov_pages.py --only 1 3 # subset by index

Run from the repo root with the app venv active (needs QDRANT_URL /
QDRANT_API_KEY / GEMINI_API_KEY in env for --write).
"""

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import trafilatura

# Make the repo root importable when run as `python scripts/ingest_denvergov_pages.py`
# (mirrors scripts/ingest_rtd_gtfs.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker.pipeline.chunker import chunk_pages
from worker.pipeline.parser import ParsedPage

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ingest_denvergov_pages")

# Honest, descriptive UA — matches the project's RTD convention; some gov
# servers 403 a default client UA.
USER_AGENT = "blue-cypher-denver-rag (sam.vburgh@gmail.com)"
SOURCE_COLLECTION = "knowledge_base"
DOC_TYPE = "denvergov_page"

# The clean static-HTML batch from ITERATION_V2.md. Checkbook (SPA) and
# Municode (huge separate corpus) are intentionally deferred — different
# ingestion shapes. `category` uses the same allowlist as the admin upload flow.
PAGES = [
    {"url": "https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Denver-City-Council/Press-Room/press-reference/city-council-backgrounder",
     "category": "council"},
    {"url": "https://www.denvergov.org/Government/Legislation-and-Transparency/Transparent-Denver",
     "category": "transparency"},
    {"url": "https://denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Department-of-Finance/Our-Divisions/Budget-and-Management-Office/City-Budget",
     "category": "budget"},
    {"url": "https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Department-of-Finance/Financial-Reports",
     "category": "finance"},
    {"url": "https://www.denvergov.org/Government/Legislation-and-Transparency/Transparent-Denver/Investments-Debt",
     "category": "finance"},
    {"url": "https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Denver-Clerk-and-Recorder/Recording-Division/find-records",
     "category": "general"},
]

# Below this many extracted chars, the page is probably JS-rendered or blocked —
# flag it rather than ingesting near-empty content.
MIN_USEFUL_CHARS = 400


def fetch_html(url: str) -> str | None:
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as exc:
        logger.warning("fetch failed: %s — %s", url, exc)
        return None


def extract_title(html: str, fallback: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return fallback
    # Gov titles are often "Page Name | City and County of Denver" — keep the head.
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title.split("|")[0].strip() or fallback


def extract_text(html: str) -> str | None:
    return trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )


def process_page(page: dict) -> dict:
    """Fetch + extract + chunk one page. Returns a report dict (no writes)."""
    url = page["url"]
    html = fetch_html(url)
    if html is None:
        return {"url": url, "ok": False, "reason": "fetch failed"}

    text = extract_text(html)
    if not text or len(text) < MIN_USEFUL_CHARS:
        return {
            "url": url,
            "ok": False,
            "reason": f"extracted only {len(text or '')} chars (likely JS-rendered/blocked)",
            "title": extract_title(html, url),
        }

    parents, children = chunk_pages([ParsedPage(page_number=1, text=text)])
    return {
        "url": url,
        "ok": True,
        "title": extract_title(html, url),
        "category": page["category"],
        "chars": len(text),
        "parents": parents,
        "children": children,
        "preview": text[:280].replace("\n", " "),
    }


def build_points(report: dict):
    """Materialize Qdrant PointStructs for a successfully-processed page.

    Imported lazily (inside --write) so a dry-run needs no Qdrant/embedding deps.
    Mirrors worker/pipeline/process.py's payload shape + adds doc_type.
    """
    from qdrant_client.http.models import PointStruct, SparseVector

    from worker.pipeline.embedder import embed_dense, embed_sparse
    from worker.pipeline.qdrant_writer import (
        DENSE_VECTOR_NAME,
        SPARSE_VECTOR_NAME,
        deterministic_point_id,
    )

    url = report["url"]
    children = report["children"]
    parent_by_index = {p.parent_index: p for p in report["parents"]}
    uploaded_at = datetime.now(timezone.utc).isoformat()

    child_texts = [c.text for c in children]
    dense_vectors = embed_dense(child_texts)
    sparse_vectors = embed_sparse(child_texts)

    points = []
    for child, dense_v, (sparse_indices, sparse_values) in zip(
        children, dense_vectors, sparse_vectors
    ):
        parent = parent_by_index[child.parent_index]
        points.append(
            PointStruct(
                id=deterministic_point_id(url, child.child_index),
                vector={
                    DENSE_VECTOR_NAME: dense_v,
                    SPARSE_VECTOR_NAME: SparseVector(
                        indices=sparse_indices, values=sparse_values
                    ),
                },
                payload={
                    "document_id": url,
                    "document_title": report["title"],
                    "original_filename": "",
                    "category": report["category"],
                    "source_url": url,
                    "source_collection": SOURCE_COLLECTION,
                    "doc_type": DOC_TYPE,
                    "uploaded_at": uploaded_at,
                    "child_index": child.child_index,
                    "child_text": child.text,
                    "child_start_page": child.start_page,
                    "child_end_page": child.end_page,
                    "parent_index": child.parent_index,
                    "parent_text": parent.text,
                    "parent_start_page": parent.start_page,
                    "parent_end_page": parent.end_page,
                },
            )
        )
    return points


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="actually upsert to Qdrant (default: dry-run, no writes)")
    ap.add_argument("--only", nargs="*", type=int, metavar="IDX",
                    help="1-based indices of PAGES to process (default: all)")
    args = ap.parse_args()

    selected = PAGES
    if args.only:
        selected = [PAGES[i - 1] for i in args.only if 1 <= i <= len(PAGES)]

    mode = "WRITE" if args.write else "DRY-RUN (no Qdrant writes)"
    print(f"\n=== denvergov.org page ingest — {mode} — {len(selected)} page(s) ===\n")

    reports = [process_page(p) for p in selected]

    ok = [r for r in reports if r["ok"]]
    bad = [r for r in reports if not r["ok"]]

    for i, r in enumerate(reports, 1):
        if r["ok"]:
            print(f"[{i}] OK  {r['title']}")
            print(f"     {r['url']}")
            print(f"     category={r['category']}  chars={r['chars']}  "
                  f"parents={len(r['parents'])}  children={len(r['children'])}")
            print(f"     preview: {r['preview']}…\n")
        else:
            print(f"[{i}] SKIP  {r['url']}")
            print(f"     reason: {r['reason']}\n")

    print(f"--- {len(ok)} extractable, {len(bad)} skipped ---")

    if not args.write:
        print("\nDry-run only. Re-run with --write to ingest the extractable pages.\n")
        return 0

    if not ok:
        print("\nNothing extractable to write. Aborting.\n")
        return 1

    from worker.pipeline.qdrant_writer import ensure_kb_collection, upsert_chunks

    ensure_kb_collection()
    total = 0
    for r in ok:
        points = build_points(r)
        upsert_chunks(points)
        total += len(points)
        print(f"  upserted {len(points)} chunks for {r['title']!r}")
    print(f"\nDone. Upserted {total} chunks across {len(ok)} page(s).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
