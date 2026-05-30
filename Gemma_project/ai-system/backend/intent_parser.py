import os
import json
from backend.models.openai_client import generate_json, INTENT_MODEL

SYSTEM_PROMPT = """You are a medical intent classifier. Extract structured data from patient requests.
Return ONLY a JSON object with these exact fields:
- task_type: one of ["appointment_booking", "prescription_refill", "referral", "lab_order", "general_inquiry"]
- specialty: medical specialty in lowercase (e.g. "cardiology", "neurology", "orthopedics", "dermatology", "general")
- urgency: one of ["routine", "urgent", "emergency"]
- symptoms: array of symptom strings mentioned (can be empty [])
- patient_id: null always (will be resolved later)

Specialty mapping rules (use the EXACT specialty string shown):
- cardiologist, heart, chest pain, palpitations, blood pressure, shortness of breath → "cardiology"
- neurologist, brain, headache, migraine, seizure, stroke, dizziness, memory loss → "neurology"
- orthopedist, bone, joint, knee, hip, spine, back pain, fracture, shoulder → "orthopedics"
- dermatologist, skin, rash, acne, eczema, psoriasis, itching, mole → "dermatology"
- psychiatrist, mental health, anxiety, depression, stress, insomnia → "psychiatry"
- ophthalmologist, eye, vision, blur, glasses → "ophthalmology"
- fever, cough, cold, flu, fatigue, general checkup → "general"
- If specialty cannot be determined, use "general"

IMPORTANT: "I need to see a cardiologist" → specialty MUST be "cardiology" not "general"."""

FALLBACK_INTENT = {
    "task_type": "appointment_booking",
    "specialty": "general",
    "urgency": "routine",
    "symptoms": [],
    "patient_id": None,
}

SPECIALTY_KEYWORDS = {
    "cardiology":    ["heart", "cardiac", "cardio", "cardiologist", "chest pain", "palpitation", "arrhythmia", "blood pressure"],
    "neurology":     ["brain", "neuro", "neurologist", "headache", "migraine", "seizure", "stroke", "dizzy", "memory"],
    "orthopedics":   ["bone", "joint", "ortho", "orthopedic", "knee", "hip", "spine", "back pain", "fracture", "shoulder"],
    "dermatology":   ["skin", "rash", "dermatology", "dermatologist", "acne", "eczema", "psoriasis", "itch", "mole", "lesion"],
    "psychiatry":    ["mental", "psychiatrist", "anxiety", "depression", "stress", "insomnia", "mood", "psychiatric"],
    "ophthalmology": ["eye", "vision", "sight", "ophthalmologist", "optometrist", "blur", "glasses", "cataract"],
    "general":       ["general", "primary", "checkup", "fever", "cold", "flu", "cough", "fatigue"],
}

URGENCY_KEYWORDS = {
    "emergency": ["emergency", "severe", "critical", "ambulance", "can't breathe", "unconscious"],
    "urgent":    ["urgent", "asap", "soon as possible", "today", "immediate", "quickly"],
}


def _rule_based_parse(text: str) -> dict:
    lower = text.lower()
    result = dict(FALLBACK_INTENT)
    result["symptoms"] = []

    result["specialty"] = _rule_based_specialty(text)

    for urgency, keywords in URGENCY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            result["urgency"] = urgency
            break

    if any(w in lower for w in ["book", "schedule", "appointment", "see a", "visit"]):
        result["task_type"] = "appointment_booking"
    elif any(w in lower for w in ["refill", "prescription", "medication", "medicine"]):
        result["task_type"] = "prescription_refill"
    elif any(w in lower for w in ["refer", "specialist", "referral"]):
        result["task_type"] = "referral"
    elif any(w in lower for w in ["lab", "blood test", "test result", "lab work"]):
        result["task_type"] = "lab_order"

    return result


def _urgency_rank(specialty: str) -> int:
    """Higher = more urgent. Used to pick the most critical specialty from multi-condition queries."""
    ranks = {
        "cardiology":    6,
        "neurology":     5,
        "orthopedics":   4,
        "dermatology":   3,
        "psychiatry":    3,
        "ophthalmology": 3,
        "general":       1,
    }
    return ranks.get(specialty, 2)


def _rule_based_specialty(text: str) -> str:
    """Returns the highest-urgency specialty found in the text, or 'general'."""
    lower = text.lower()
    found = []
    for specialty, keywords in SPECIALTY_KEYWORDS.items():
        if specialty == "general":
            continue
        if any(kw in lower for kw in keywords):
            found.append(specialty)
    if not found:
        return "general"
    return max(found, key=_urgency_rank)


def parse_intent(user_text: str) -> dict:
    prompt = f"""Patient request: "{user_text}"

Extract the intent and return a JSON object."""

    rule_specialty = _rule_based_specialty(user_text)

    try:
        result = generate_json(INTENT_MODEL, prompt, SYSTEM_PROMPT, retries=3)
        required = ["task_type", "specialty", "urgency", "symptoms", "patient_id"]
        for field in required:
            if field not in result:
                raise ValueError(f"Missing field: {field}")
        result["patient_id"] = None

        # If LLM returned "general" but rules detected a specific specialty, trust rules.
        # Small LLMs often default to "general" for multi-condition or ambiguous queries.
        if result.get("specialty") == "general" and rule_specialty != "general":
            print(f"[IntentParser] LLM said 'general' but rules detected '{rule_specialty}' — overriding")
            result["specialty"] = rule_specialty

        return result
    except Exception as e:
        print(f"[IntentParser] LLM failed ({e}), using rule-based fallback")
        return _rule_based_parse(user_text)


if __name__ == "__main__":
    test_inputs = [
        "I need to book an appointment with a cardiologist for chest pain",
        "I have a severe headache and dizziness, need to see someone urgently",
        "I'd like a general checkup, routine appointment please",
    ]
    print("=== Intent Parser Test ===\n")
    for text in test_inputs:
        result = parse_intent(text)
        print(f"Input:  {text}")
        print(f"Output: {json.dumps(result, indent=2)}\n")
