import os
import json
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue
from dotenv import load_dotenv
from backend.models.openai_client import embed

load_dotenv()

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "medical_secret_key")
COLLECTION_NAME = "medical_knowledge"
VECTOR_SIZE = 768


def get_client() -> QdrantClient:
    kwargs = {"host": QDRANT_HOST, "port": QDRANT_PORT, "timeout": 30, "https": False}
    if QDRANT_API_KEY:
        kwargs["api_key"] = QDRANT_API_KEY
    return QdrantClient(**kwargs)


def ensure_collection() -> None:
    client = get_client()
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Created Qdrant collection: {COLLECTION_NAME}")
    else:
        print(f"Qdrant collection already exists: {COLLECTION_NAME}")


def ingest_documents(docs: list[dict]) -> int:
    """
    docs: list of {"text": str, "metadata": dict}
    Returns number of documents ingested.
    """
    client = get_client()
    ensure_collection()

    points = []
    for i, doc in enumerate(docs):
        vector = embed(doc["text"])
        if not vector:
            continue
        points.append(
            PointStruct(
                id=i,
                vector=vector,
                payload={
                    "text": doc["text"],
                    **doc.get("metadata", {}),
                },
            )
        )

    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)


def search(query: str, top_k: int = 10, category: str | None = None) -> list[dict]:
    client = get_client()
    query_vector = embed(query)
    if not query_vector:
        return []

    search_filter = None
    if category:
        search_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=top_k,
        query_filter=search_filter,
        with_payload=True,
    )
    return [
        {"text": r.payload.get("text", ""), "score": r.score, "metadata": r.payload}
        for r in results
    ]


def load_and_ingest_all(data_dir: str = "data/medical") -> None:
    data_path = Path(data_dir)
    docs = []

    # Load ICD-10 sample
    icd_file = data_path / "icd10_sample.json"
    if icd_file.exists():
        with open(icd_file) as f:
            icd_data = json.load(f)
        for entry in icd_data:
            text = f"ICD-10 Code {entry['code']}: {entry['description']}. Category: {entry.get('category', '')}. Symptoms: {entry.get('symptoms', '')}."
            docs.append({"text": text, "metadata": {"category": "icd10", "code": entry["code"]}})

    # Load policies
    policies_file = data_path / "policies.txt"
    if policies_file.exists():
        with open(policies_file) as f:
            content = f.read()
        # Split into individual policies by double newline
        chunks = [p.strip() for p in content.split("\n\n") if p.strip()]
        for chunk in chunks:
            docs.append({"text": chunk, "metadata": {"category": "policy"}})

    total = ingest_documents(docs)
    print(f"Ingested {total} documents into Qdrant")
