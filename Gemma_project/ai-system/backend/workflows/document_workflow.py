from backend.models.openai_client import generate, WORKFLOW_MODEL

SYSTEM_PROMPT = (
    "You are a document analysis assistant. Answer questions based ONLY on the provided "
    "document context. If the answer is not in the document, clearly say 'This information "
    "is not found in the uploaded document.' Be precise and quote relevant sections when helpful."
)


def _build_context(history: list[dict]) -> str:
    lines = []
    for msg in history[-4:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(lines)


def run(user_text: str, document_id: str, history: list[dict] | None = None) -> dict:
    history = history or []
    sources: list[str] = []

    try:
        from backend.agents.rag_agent import search_document
        results = search_document(user_text, document_id, top_k=5)
        chunks = [r["text"] for r in results if r.get("score", 0) > 0.3]
        sources = chunks[:3]
    except Exception:
        chunks = []

    conv_context = _build_context(history)

    if chunks:
        doc_context = "\n\n---\n\n".join(chunks)
        prompt = (
            f"Document content:\n{doc_context}\n\n"
            f"{conv_context}\nUser: {user_text}\nAssistant:"
        ).strip()
    else:
        prompt = (
            f"Note: No relevant content found in document for this query.\n\n"
            f"{conv_context}\nUser: {user_text}\nAssistant:"
        ).strip()

    response = generate(WORKFLOW_MODEL, prompt, SYSTEM_PROMPT)
    return {
        "response": response.strip(),
        "workflow": "document_chat",
        "sources": sources,
    }
