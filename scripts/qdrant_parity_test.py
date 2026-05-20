"""Phase 5 parity test — Qdrant local vs Qdrant Cloud.

Runs the same hybrid search (dense gemini-embedding-001 + sparse BM25) against
both clusters with the same query set, then compares top-K results.

Acceptance criteria (from next_focus_deployment.md):
  - top-3 identical (same IDs in same order)
  - top-10 ≥90% overlap (same ID set, order may vary)
  - 5 random spot-checks: payloads byte-identical between clusters

Reads creds:
  QDRANT_URL                (local, default http://localhost:6333)
  QDRANT_API_KEY            (local, usually unset)
  QDRANT_PROD_URL           (from .env.production)
  QDRANT_PROD_API_KEY       (from .env.production)
  QDRANT_COLLECTION_NAME    (default denver_gis_catalog)
  GEMINI_API_KEY            (for embeddings)

Run with:
  set -a && source .env && source .env.production && set +a && \
    python3 scripts/qdrant_parity_test.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from dataclasses import dataclass

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient

COLLECTION = os.getenv("QDRANT_COLLECTION_NAME", "denver_gis_catalog")
TOP_K = 10

# Queries chosen to exercise every doc_type at least once.
QUERIES = [
    "parks in Capitol Hill",
    "crime statistics in Five Points",
    "public schools serving LoDo",
    "private schools in Denver",
    "RTD bus stops near Union Station",
    "light rail routes to DIA",
    "demographics of Cherry Creek",
    "recreation centers downtown",
    "library hours Park Hill",
    "traffic accidents Globeville",
    "building permits in RiNo",
    "property tax records",
    "affordable housing Denver",
    "population of Whittier",
    "open data catalog parks",
]


@dataclass
class Hit:
    id: str
    score: float


def _store(url: str, api_key: str | None) -> QdrantVectorStore:
    dense = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    sparse = FastEmbedSparse(model_name="Qdrant/bm25")
    return QdrantVectorStore.from_existing_collection(
        embedding=dense,
        sparse_embedding=sparse,
        url=url,
        api_key=api_key,
        collection_name=COLLECTION,
        retrieval_mode=RetrievalMode.HYBRID,
    )


def _top_k(store: QdrantVectorStore, query: str) -> list[Hit]:
    results = store.similarity_search_with_score(query, k=TOP_K)
    return [Hit(id=str(doc.metadata.get("_id", doc.id)), score=score) for doc, score in results]


def _overlap(a: list[Hit], b: list[Hit]) -> float:
    return len({h.id for h in a} & {h.id for h in b}) / max(len(a), 1)


def _spot_check_payloads(local: QdrantClient, cloud: QdrantClient, ids: list[str]) -> list[tuple[str, bool]]:
    out = []
    for pid in ids:
        l = local.retrieve(collection_name=COLLECTION, ids=[pid], with_payload=True, with_vectors=False)
        c = cloud.retrieve(collection_name=COLLECTION, ids=[pid], with_payload=True, with_vectors=False)
        if not l or not c:
            out.append((pid, False))
            continue
        # Compare via canonical JSON dump
        lp = json.dumps(l[0].payload, sort_keys=True, default=str)
        cp = json.dumps(c[0].payload, sort_keys=True, default=str)
        out.append((pid, lp == cp))
    return out


def main() -> int:
    local_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    local_key = os.environ.get("QDRANT_API_KEY") or None
    cloud_url = os.environ["QDRANT_PROD_URL"]
    cloud_key = os.environ["QDRANT_PROD_API_KEY"]

    print(f"Local:  {local_url}")
    print(f"Cloud:  {cloud_url}")
    print(f"Top-K:  {TOP_K}, queries: {len(QUERIES)}\n")

    local_store = _store(local_url, local_key)
    cloud_store = _store(cloud_url, cloud_key)

    pass_top3 = 0
    pass_top10 = 0
    failures: list[tuple[str, str]] = []

    for q in QUERIES:
        local_hits = _top_k(local_store, q)
        cloud_hits = _top_k(cloud_store, q)
        top3_ids_local = [h.id for h in local_hits[:3]]
        top3_ids_cloud = [h.id for h in cloud_hits[:3]]
        top3_ok = top3_ids_local == top3_ids_cloud
        top10_overlap = _overlap(local_hits, cloud_hits)
        top10_ok = top10_overlap >= 0.9

        status = "PASS" if (top3_ok and top10_ok) else "FAIL"
        print(f"[{status}] {q!r:<55} top3_ident={top3_ok}  top10_overlap={top10_overlap:.0%}")

        if top3_ok:
            pass_top3 += 1
        if top10_ok:
            pass_top10 += 1
        if not (top3_ok and top10_ok):
            failures.append((q, f"top3 local={top3_ids_local} cloud={top3_ids_cloud} top10_overlap={top10_overlap:.0%}"))

    # Spot-check 5 random points by ID
    print()
    print("--- Payload spot-check (5 random IDs) ---")
    local_client = QdrantClient(url=local_url, api_key=local_key)
    cloud_client = QdrantClient(url=cloud_url, api_key=cloud_key)
    sample, _ = local_client.scroll(collection_name=COLLECTION, limit=500, with_payload=False, with_vectors=False)
    rng = random.Random(42)
    chosen_ids = [str(p.id) for p in rng.sample(sample, k=5)]
    spot_results = _spot_check_payloads(local_client, cloud_client, chosen_ids)
    for pid, ok in spot_results:
        print(f"  [{'OK' if ok else 'DIFF'}] {pid}")

    spot_ok = all(ok for _, ok in spot_results)

    print()
    print(f"Summary: top-3 identical {pass_top3}/{len(QUERIES)}  |  "
          f"top-10 ≥90% overlap {pass_top10}/{len(QUERIES)}  |  "
          f"spot-check payloads {sum(1 for _, ok in spot_results if ok)}/5")

    accept = pass_top3 == len(QUERIES) and pass_top10 == len(QUERIES) and spot_ok
    if accept:
        print("\nACCEPT — safe to proceed to Phase 6.")
        return 0
    print("\nREJECT — investigate before touching env vars.")
    for q, detail in failures:
        print(f"  {q!r}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
