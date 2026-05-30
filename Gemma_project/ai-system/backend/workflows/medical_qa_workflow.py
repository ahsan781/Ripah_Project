"""
Medical Q&A workflow — RAG-augmented health questions with full conversation context.
"""

from backend.models.openai_client import chat_with_history, WORKFLOW_MODEL
from backend.prompts.guardrails import check
from backend.rag.embeddings import search

SYSTEM_PROMPT = """You are a medical AI assistant.

Your role:
- Answer health and medical questions clearly and accurately
- Help patients understand symptoms, conditions, treatments, and medications
- Guide users on whether to seek urgent, routine, or specialist care
- Book and manage doctor appointments when asked

Conversation rules:
- ALWAYS reference prior turns in the conversation when relevant
  (e.g. "Based on the chest pain you mentioned earlier...")
- If the user asks a vague follow-up ("tell me more", "what about that?"), refer back to your last answer
- Keep answers factual — never fabricate drug names, dosages, or clinical guidelines
- For serious symptoms (chest pain, stroke signs, difficulty breathing), always advise immediate medical attention
- End clinical answers with: "Please consult a qualified healthcare professional before making medical decisions."

When asked to book an appointment, ask:
1. What specialty do they need? (cardiology, neurology, general, etc.)
2. How urgent? (routine / urgent / emergency)
3. Patient name

Context from knowledge base (use when relevant, ignore if empty):
{rag_context}
"""


def run(user_text: str, history: list[dict] | None = None) -> dict:
    history = history or []

    # Guardrails
    guard = check(user_text, domain="medical")
    if guard.blocked:
        return {
            "response": guard.reply,
            "workflow": "medical_qa",
            "sources":  [],
            "guardrail": guard.category,
        }

    # RAG retrieval
    sources: list[str] = []
    rag_context = ""
    try:
        rag_results = search(user_text, top_k=5)
        chunks = [r["text"] for r in rag_results if r.get("score", 0) > 0.4]
        sources = chunks[:3]
        rag_context = "\n\n".join(chunks) if chunks else "No relevant medical knowledge found."
    except Exception:
        rag_context = ""

    system = SYSTEM_PROMPT.replace("{rag_context}", rag_context)

    try:
        response = chat_with_history(
            model=WORKFLOW_MODEL,
            system_prompt=system,
            history=history,
            user_message=user_text,
        )
    except Exception as exc:
        return {
            "response": "I'm having trouble connecting right now. Please try again in a moment.",
            "workflow": "medical_qa",
            "sources":  [],
            "error":    str(exc),
        }

    return {
        "response": response,
        "workflow": "medical_qa",
        "sources":  sources,
    }
