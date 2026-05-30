"""
Property Information workflow.

Helps users find housing near Riphah campuses, on-campus hostels,
rental properties, and general real estate queries in Pakistan.
Searches `property_knowledge` Qdrant collection when available.
"""

from backend.models.openai_client import generate, WORKFLOW_MODEL, embed
from backend.rag.embeddings import get_client

COLLECTION_NAME = "property_knowledge"

SYSTEM_PROMPT = (
    "You are a Property Information Assistant for Riphah International University. "
    "Help students, faculty, and staff find housing options near Riphah campuses: "
    "Islamabad (G-7/4, Blue Area area), Lahore, Faisalabad, Rawalpindi, and Peshawar. "
    "\n\n"
    "Answer questions about:\n"
    "- On-campus hostels and dormitories (male/female)\n"
    "- Nearby rental apartments, rooms, PGs, and shared housing\n"
    "- Estimated rent ranges per city\n"
    "- Transport/commute options from nearby areas\n"
    "- General property market advice for Pakistan\n"
    "\n"
    "For specific listings, always recommend: zameen.com, property.pk, or OLX Pakistan. "
    "If hostel availability or exact fees are unknown, direct users to the campus housing office "
    "or the Riphah admissions/admin office. Be practical and location-specific."
)


def _search_property_knowledge(query: str, top_k: int = 4) -> list[str]:
    try:
        client = get_client()
        collections = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in collections:
            return []
        query_vector = embed(query)
        if not query_vector:
            return []
        results = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [r.payload.get("text", "") for r in results if r.score > 0.4]
    except Exception:
        return []


def _build_context(history: list[dict]) -> str:
    lines = []
    for msg in history[-4:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)


def run(user_text: str, history: list[dict] | None = None) -> dict:
    history = history or []
    chunks = _search_property_knowledge(user_text)
    sources = chunks[:3]
    conv_context = _build_context(history)

    if chunks:
        context = "\n\n".join(chunks)
        prompt = (
            f"Property context:\n{context}\n\n"
            f"{conv_context}\nUser: {user_text}\nAssistant:"
        ).strip()
    else:
        prompt = (f"{conv_context}\nUser: {user_text}\nAssistant:").strip()

    response = generate(WORKFLOW_MODEL, prompt, SYSTEM_PROMPT)
    return {
        "response": response.strip(),
        "workflow": "property",
        "sources": sources,
    }
