"""
Universal Guardrails — applied to every workflow before hitting the LLM.

Layers:
  1. Hard block   — harmful/jailbreak content, never reaches LLM
  2. Soft deflect — off-topic, return canned reply
  3. Context gate — check if message is coherent and within domain

Usage:
    from backend.prompts.guardrails import check, GuardResult

    result = check(user_text, domain="medical")
    if result.blocked:
        return {"response": result.reply, "guardrail": result.category}
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Domain-specific out-of-scope replies
# ---------------------------------------------------------------------------

_DOMAIN_SCOPE = {
    "general":     "I'm your AI assistant. I can help with medical appointments, university admissions, HR queries, and general questions.",
    "medical":     "I can help with medical appointments, health questions, and doctor availability. What can I assist with?",
    "university":  "I'm the Riphah University assistant. I help with admissions, programs, fees, and student services.",
    "hr":          "I'm the HR assistant. I help with leave, payroll, policies, and employee services.",
    "property":    "I can help with student housing, hostels, and accommodation near campus.",
    "document":    "I can answer questions about your uploaded document. Please ask something specific about it.",
}


# ---------------------------------------------------------------------------
# Hard blocks (apply to ALL domains)
# ---------------------------------------------------------------------------

_HARD_BLOCKS: list[tuple[re.Pattern, str, str]] = [
    # (pattern, category, reply)
    (re.compile(r"ignore\s+(all|previous|your)\s+(instructions?|rules?|prompt)", re.I),
     "jailbreak", "I can't do that. How can I help you with a legitimate question?"),

    (re.compile(r"(you are now|pretend (you are|to be)|act as (a|an|if)|roleplay as|DAN mode|developer mode)", re.I),
     "persona_override", "I'm your AI assistant and I maintain my guidelines. What can I help you with?"),

    (re.compile(r"(reveal|print|output|repeat|show)\s+(your\s+)?(system\s+prompt|instructions|training data|rules)", re.I),
     "prompt_extraction", "I can't share my internal configuration. I'm happy to help with your actual question."),

    (re.compile(r"\b(bomb|weapon|explosiv|terror(ist)?|mass kill|suicide\s+method|how\s+to\s+kill)\b", re.I),
     "harmful", "I can't help with that. Please reach out to emergency services if this is urgent."),

    (re.compile(r"\b(sql\s*injection|xss\s*attack|remote\s*code|exploit\s+this|bypass\s+auth)\b", re.I),
     "security_attack", "That's outside what I can help with."),

    (re.compile(r"\b(porn|xxx|adult\s+content|nsfw|sexual\s+content)\b", re.I),
     "adult_content", "I can't help with that."),
]


# ---------------------------------------------------------------------------
# Domain soft-deflects (per-domain off-topic patterns)
# ---------------------------------------------------------------------------

_SOFT_DEFLECTS: dict[str, list[tuple[re.Pattern, str]]] = {
    "medical": [
        (re.compile(r"\b(bitcoin|crypto|trading|forex|nft|stock\s+market)\b", re.I),
         "I'm a medical assistant. I can help with appointments and health questions — not financial topics."),
        (re.compile(r"\b(politics|election|PTI|PMLN|government\s+policy)\b", re.I),
         "I focus on medical topics. What health question can I help with?"),
    ],
    "university": [
        (re.compile(r"\b(NUST|FAST|LUMS|COMSATS|UET|IBA|NED|other\s+university)\b", re.I),
         "I only have information about Riphah International University. For other universities, please visit their official websites."),
        (re.compile(r"\b(bitcoin|crypto|forex|trading|nft)\b", re.I),
         "I'm the Riphah University assistant — admissions, programs, and student services only."),
        (re.compile(r"\b(politics|election|PTI|PMLN)\b", re.I),
         "I don't discuss politics. I'm here for Riphah University questions."),
        (re.compile(r"\b(religion\s+debate|fatwa|sect|shia|sunni|kafir)\b", re.I),
         "I don't engage in religious debates. I can help with RIU's Islamic Studies programs if that's what you're looking for."),
    ],
    "hr": [
        (re.compile(r"\b(bitcoin|crypto|trading|investment)\b", re.I),
         "I'm the HR assistant. I help with leave, payroll, and workplace policies."),
        (re.compile(r"\b(medical|doctor|appointment|symptoms)\b", re.I),
         "For medical help, please switch to the Medical section. I handle HR matters like leave, payroll, and benefits."),
    ],
}


# ---------------------------------------------------------------------------
# Generic nonsense / empty intent detector
# ---------------------------------------------------------------------------

_NONSENSE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^[^a-zA-Z؀-ۿ]{0,3}$"),             # only symbols/numbers
    re.compile(r"^(.)\1{6,}$"),                                 # repeating chars: "aaaaaaa"
    re.compile(r"^(test|hello|hi|hey|yo|ping|ok|okay|sure)$", re.I),  # pure greetings (handled as general)
]

_GREETING_PATTERNS = re.compile(
    r"^(hi|hello|hey|good\s*(morning|afternoon|evening)|assalam|salam|howdy|greetings)[!.,?\s]*$",
    re.I,
)

_GREETING_REPLIES: dict[str, str] = {
    "general":    "Hello! How can I help you today? I can assist with medical appointments, admissions, HR queries, and more.",
    "medical":    "Hello! I'm your medical assistant. How can I help you — would you like to book an appointment or have a health question?",
    "university": "Assalam u Alaikum! I'm AskRiphah, Riphah University's assistant. Ask me about admissions, programs, fees, or campus life.",
    "hr":         "Hello! I'm the HR assistant. I can help with leave requests, payroll, policies, and benefits.",
    "property":   "Hello! Looking for student accommodation near campus? I can help.",
    "document":   "Hello! I have your document loaded. Ask me anything about its contents.",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class GuardResult:
    blocked:  bool
    category: str = ""
    reply:    str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(user_text: str, domain: str = "general") -> GuardResult:
    """
    Run all guardrail layers against user_text for the given domain.

    Returns GuardResult(blocked=True, reply=...) if the message should be
    intercepted before reaching the LLM.

    Returns GuardResult(blocked=False) when the message is safe to process.
    """
    text = (user_text or "").strip()

    if not text:
        return GuardResult(
            blocked=True,
            category="empty",
            reply="Please type your question and I'll be happy to help.",
        )

    # ── Greeting shortcut (no LLM needed) ────────────────────────────────────
    if _GREETING_PATTERNS.match(text):
        return GuardResult(
            blocked=True,
            category="greeting",
            reply=_GREETING_REPLIES.get(domain, _GREETING_REPLIES["general"]),
        )

    # ── Hard blocks (all domains) ─────────────────────────────────────────────
    for pattern, category, reply in _HARD_BLOCKS:
        if pattern.search(text):
            return GuardResult(blocked=True, category=category, reply=reply)

    # ── Domain-specific soft deflects ─────────────────────────────────────────
    for pattern, reply in _SOFT_DEFLECTS.get(domain, []):
        if pattern.search(text):
            return GuardResult(blocked=True, category="off_topic", reply=reply)

    return GuardResult(blocked=False)


def off_topic_reply(domain: str = "general") -> str:
    """Return the standard scope reminder for a domain."""
    return _DOMAIN_SCOPE.get(domain, _DOMAIN_SCOPE["general"])
