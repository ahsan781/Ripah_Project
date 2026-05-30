from qdrant_client.models import Distance, VectorParams, PointStruct
from backend.rag.embeddings import get_client
from backend.models.openai_client import embed
from backend.file_processing.chunker import chunk_text

COLLECTION_NAME = "hr_knowledge"
VECTOR_SIZE = 768
AGENT_NAME = "hr_agent"


def _ensure_collection() -> None:
    client = get_client()
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"[{AGENT_NAME}] Created Qdrant collection: {COLLECTION_NAME}")


def ingest_hr_document(text: str, filename: str) -> int:
    _ensure_collection()
    client = get_client()
    chunks = chunk_text(text, chunk_size=400, overlap=40)
    points = []
    for i, chunk in enumerate(chunks):
        vector = embed(chunk)
        if not vector:
            continue
        points.append(
            PointStruct(
                id=abs(hash(f"{filename}_{i}")) % (2**63),
                vector=vector,
                payload={"text": chunk, "filename": filename, "chunk_index": i},
            )
        )
    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)
    print(f"[{AGENT_NAME}] Ingested {len(points)} chunks from '{filename}' into hr_knowledge")
    return len(points)
