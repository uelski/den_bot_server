import json
import os
from langchain_qdrant import QdrantVectorStore, RetrievalMode, FastEmbedSparse
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document

# 1. Configuration - Point to your local Docker Qdrant
QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "denver_gis_catalog"

def ingest_data():
    # 2. Initialize Embeddings
    # Dense: Captures meaning (2026 stable version)
    dense_embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    
    # Sparse: Captures keywords (BM25) - Runs locally via FastEmbed
    sparse_embeddings = FastEmbedSparse(model_name="Qdrant/bm25")

    # 3. Load your cleaned JSON
    with open("enriched_denver_catalog_cleaned.json", "r") as f:
        raw_data = json.load(f)

    print(f"📂 Loaded {len(raw_data)} services. Preparing documents...")

    # 4. Create LangChain Documents
    docs = []
    for item in raw_data:
        # We search against the summary, but store EVERYTHING in metadata
        doc = Document(
            page_content=item['semantic_summary'],
            metadata={
                "service_name": item['service_name'],
                "agency": item.get('agency', 'Unknown'),
                "full_metadata": json.dumps(item) # The "Parent" data for the Agent
            }
        )
        docs.append(doc)

    # 5. Ingest into Qdrant with Hybrid Mode enabled
    print(f"🚀 Ingesting into Qdrant at {QDRANT_URL}...")
    
    vector_store = QdrantVectorStore.from_documents(
        documents=docs,
        embedding=dense_embeddings,
        sparse_embedding=sparse_embeddings,
        url=QDRANT_URL,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
        force_recreate=True  # Useful for testing; set to False later
    )

    print(f"✅ Success! Hybrid index '{COLLECTION_NAME}' is ready.")

if __name__ == "__main__":
    ingest_data()