from backend.models.openai_client import generate_json, INTENT_MODEL
from backend.prompts.university_prompts import ROUTER_SYSTEM

KEYWORD_ROUTES: dict[str, list[str]] = {
    "student_data": [
        "show me student", "student data", "student record", "student profile",
        "student information", "data of student", "info of student",
        "show data of", "show record of", "show profile of",
        "cgpa of", "cgpa is", "what is cgpa", "what's cgpa",
        "semester of", "which semester", "attendance of", "attendance for",
        "courses of", "courses for", "enrolled in", "registration number of",
        "grades of", "marks of", "results of", "academic record",
        "list all students", "list students", "all students", "students in department",
        "show all students", "student list",
    ],
    "medical_appointment": [
        "book appointment", "schedule appointment", "see a doctor",
        "cancel appointment", "book a slot", "doctor appointment",
        "reschedule", "available slots", "book a doctor",
    ],
    "hr_tasks": [
        "leave", "salary", "payroll", "hr policy", "employee",
        "attendance", "benefits", "vacation", "sick leave",
        "annual leave", "hr", "human resources", "pay slip",
        "resignation", "onboarding", "appraisal",
    ],
    "medical_qa": [
        "symptoms", "treatment", "diagnosis", "medication",
        "health advice", "disease", "drug", "medicine",
        "what is the cure", "side effects", "prescription",
        "how to treat", "what causes",
    ],
    "university": [
        "admission", "admissions", "apply", "merit", "eligibility",
        "course", "courses", "syllabus", "syllabi", "semester", "credit hours",
        "registrar", "registration", "drop a course", "add a course", "transcript",
        "gpa", "academic calendar", "dean", "professor", "lecture", "assignment",
        "thesis", "dissertation", "scholarship", "financial aid", "tuition",
        "fee structure", "program", "programs", "degree", "mbbs", "bds", "pharm",
        "campus", "dormitory", "dorm", "student id", "academic advisor",
        "research grant", "faculty", "convocation", "graduation", "riphah",
        "university", "department", "bs ", "ms ", "phd", "undergraduate", "postgraduate",
    ],
    "property": [
        "hostel", "housing", "rental", "rent", "apartment", "flat", "pg",
        "shared room", "accommodation", "dormitory", "dorm", "lodging",
        "near campus", "near riphah", "student housing", "zameen", "property",
    ],
    "document_chat": [
        "document", "pdf", "file", "uploaded", "according to",
        "in the file", "based on the document", "what does the file say",
        "summarize the document", "from the report",
    ],
}

# When a category is set, these are the only allowed workflows per domain
_CATEGORY_ALLOWED: dict[str, set[str]] = {
    "university": {"university", "document_chat"},
    "medical":    {"medical_qa", "medical_appointment", "document_chat"},
    "property":   {"property", "document_chat"},
}

# Within the medical category, these keywords force appointment routing
_APPOINTMENT_KEYWORDS = [
    "book", "schedule", "appointment", "see a doctor", "available slot",
    "reschedule", "cancel appointment", "reserve", "book a doctor",
]


def _keyword_route(text: str) -> str | None:
    lower = text.lower()
    for workflow, keywords in KEYWORD_ROUTES.items():
        if any(kw in lower for kw in keywords):
            return workflow
    return None


def route_query(
    user_text: str,
    has_active_document: bool = False,
    category: str | None = None,
) -> str:
    if has_active_document:
        return "document_chat"

    # ── Category-constrained routing ────────────────────────────────────
    if category == "university":
        return "university"

    if category == "property":
        return "property"

    if category == "medical":
        lower = user_text.lower()
        if any(kw in lower for kw in _APPOINTMENT_KEYWORDS):
            return "medical_appointment"
        return "medical_qa"

    # ── Free routing (no category) ───────────────────────────────────────
    keyword_result = _keyword_route(user_text)

    try:
        result = generate_json(
            INTENT_MODEL,
            f'User message: "{user_text}"\nClassify this into one workflow category.',
            ROUTER_SYSTEM,
            retries=2,
        )
        workflow = result.get("workflow", "general")
        valid = {
            "student_data", "medical_appointment", "hr_tasks", "medical_qa",
            "university", "property", "document_chat", "general",
        }
        if workflow in valid:
            return workflow
    except Exception:
        pass

    return keyword_result or "general"
