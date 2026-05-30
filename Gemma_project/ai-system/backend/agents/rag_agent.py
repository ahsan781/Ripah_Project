from qdrant_client.models import Distance, VectorParams, PointStruct
from backend.rag.embeddings import get_client
from backend.models.openai_client import embed
from backend.file_processing.chunker import chunk_text

VECTOR_SIZE = 768
AGENT_NAME = "rag_agent"


def _collection_name(document_id: str) -> str:
    return f"doc_{document_id}"


def _ensure_collection(document_id: str) -> None:
    client = get_client()
    name = _collection_name(document_id)
    collections = [c.name for c in client.get_collections().collections]
    if name not in collections:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def ingest_document(text: str, document_id: str, filename: str) -> int:
    _ensure_collection(document_id)
    client = get_client()
    collection = _collection_name(document_id)
    chunks = chunk_text(text, chunk_size=400, overlap=40)
    points = []
    for i, chunk in enumerate(chunks):
        vector = embed(chunk)
        if not vector:
            continue
        points.append(
            PointStruct(
                id=i,
                vector=vector,
                payload={"text": chunk, "filename": filename, "chunk_index": i},
            )
        )
    if points:
        client.upsert(collection_name=collection, points=points)
    print(f"[{AGENT_NAME}] Ingested {len(points)} chunks for doc '{document_id}'")
    return len(points)


def search_document(query: str, document_id: str, top_k: int = 5) -> list[dict]:
    client = get_client()
    collection = _collection_name(document_id)
    collections = [c.name for c in client.get_collections().collections]
    if collection not in collections:
        return []
    query_vector = embed(query)
    if not query_vector:
        return []
    results = client.search(
        collection_name=collection,
        query_vector=query_vector,
        limit=top_k,
        with_payload=True,
    )
    return [{"text": r.payload.get("text", ""), "score": r.score} for r in results]


def delete_document(document_id: str) -> None:
    client = get_client()
    collection = _collection_name(document_id)
    collections = [c.name for c in client.get_collections().collections]
    if collection in collections:
        client.delete_collection(collection_name=collection)
        print(f"[{AGENT_NAME}] Deleted collection {collection}")
