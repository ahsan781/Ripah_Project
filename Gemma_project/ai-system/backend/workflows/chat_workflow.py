"""
General chat workflow — multi-turn conversation with full context.
"""

from backend.models.openai_client import chat_with_history, WORKFLOW_MODEL
from backend.prompts.guardrails import check

SYSTEM_PROMPT = """You are a helpful AI assistant for a medical and university platform.

You help users with:
- Medical appointments and health questions
- University admissions (Riphah International University)
- HR queries (leave, payroll, policies)
- General questions and task automation

Guidelines:
- Always remember and reference what the user said earlier in the conversation
- If a user asks a follow-up like "what about the other one?" or "can you explain more?", refer back to your previous answer
- Keep responses clear, concise, and friendly
- If you're unsure, say so honestly rather than guessing
- For medical advice, always recommend consulting a qualified professional
- Never make up facts — if you don't know, say so
"""


def run(user_text: str, history: list[dict] | None = None) -> dict:
    history = history or []

    # Guardrails
    guard = check(user_text, domain="general")
    if guard.blocked:
        return {
            "response": guard.reply,
            "workflow": "general",
            "sources":  [],
            "guardrail": guard.category,
        }

    try:
        response = chat_with_history(
            model=WORKFLOW_MODEL,
            system_prompt=SYSTEM_PROMPT,
            history=history,
            user_message=user_text,
        )
    except Exception as exc:
        return {
            "response": "I'm having trouble connecting right now. Please try again in a moment.",
            "workflow": "general",
            "sources":  [],
            "error":    str(exc),
        }

    return {
        "response": response,
        "workflow": "general",
        "sources":  [],
    }
