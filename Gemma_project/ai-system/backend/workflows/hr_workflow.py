"""
HR workflow — employee queries with full conversation context and guardrails.
"""

from backend.models.openai_client import chat_with_history, WORKFLOW_MODEL
from backend.prompts.guardrails import check
from backend.rag.embeddings import get_client

COLLECTION_NAME = "hr_knowledge"

SYSTEM_PROMPT = """You are a professional HR AI assistant.

Your role:
- Help employees with leave requests, payroll queries, company policies
- Explain benefits, attendance rules, performance reviews, onboarding
- Guide managers on HR processes and compliance
- Assist with resignation, transfer, and promotion procedures

Conversation rules:
- ALWAYS maintain context across the full conversation
- If an employee said "I need 3 days off next week" earlier, remember that when they ask follow-up questions
- Be empathetic and professional — HR issues can be sensitive
- Reference specific policy sections when available from the knowledge base
- If a policy detail is not in the knowledge base, give best-practice guidance and note it should be confirmed with HR
- Never fabricate specific leave balances, salary figures, or policy clauses

HR Policy context from knowledge base (use when available):
{hr_context}
"""


def _search_hr_knowledge(query: str, top_k: int = 5) -> list[str]:
    try:
        from backend.models.openai_client import embed
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


def run(user_text: str, history: list[dict] | None = None) -> dict:
    history = history or []

    # Guardrails
    guard = check(user_text, domain="hr")
    if guard.blocked:
        return {
            "response": guard.reply,
            "workflow": "hr_tasks",
            "sources":  [],
            "guardrail": guard.category,
        }

    # RAG retrieval
    chunks = _search_hr_knowledge(user_text)
    sources = chunks[:3]
    hr_context = "\n\n".join(chunks) if chunks else "No HR policy documents loaded yet."

    system = SYSTEM_PROMPT.replace("{hr_context}", hr_context)

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
            "workflow": "hr_tasks",
            "sources":  [],
            "error":    str(exc),
        }

    return {
        "response": response,
        "workflow": "hr_tasks",
        "sources":  sources,
    }
