"""Retrieval helpers shared by graph nodes.

`kb.py` — hybrid search against the PDF knowledge-base collection
(`denver_pdf_knowledge_base`), which the worker writes with a flat payload
and named `dense`/`sparse` vectors. That shape is NOT LangChain-managed, so
it can't reuse the catalog's `QdrantVectorStore`; this package talks to
`qdrant_client` directly and maps results into LangChain `Document`s.
"""
