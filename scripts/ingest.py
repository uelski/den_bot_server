import json
import os
from dotenv import load_dotenv
from langchain_qdrant import QdrantVectorStore, RetrievalMode, FastEmbedSparse
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document

load_dotenv()

# 1. Configuration
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = "denver_gis_catalog"

def ingest_data():
    # 2. Initialize Embeddings
    # Dense: Captures meaning (2026 stable version)
    dense_embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
    
    # Sparse: Captures keywords (BM25) - Runs locally via FastEmbed
    sparse_embeddings = FastEmbedSparse(model_name="Qdrant/bm25")

    # 3. Load your cleaned JSON
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_PATH = os.path.join(BASE_DIR, "..", "data", "enriched_denver_catalog_cleaned.json")
    with open(DATA_PATH, "r") as f:
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
                "base_url": item['base_url'],
                "has_layers": len(item.get('layers', [])) > 0,
                "full_metadata": json.dumps(item)
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
        api_key=QDRANT_API_KEY,
        collection_name=COLLECTION_NAME,
        retrieval_mode=RetrievalMode.HYBRID,
        force_recreate=True  # Useful for testing; set to False later
    )

    print(f"✅ Success! Hybrid index '{COLLECTION_NAME}' is ready.")

if __name__ == "__main__":
    ingest_data()