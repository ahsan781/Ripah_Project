"""
Student data workflow.

Handles queries like:
  "Show me the data of student Awais"
  "What is Sara's CGPA?"
  "Which courses is Bilal taking this semester?"
  "Show attendance of Fatima"
  "List all CS students"

Flow:
  1. Extract student name / reg_no from the query
  2. Fetch structured data from PostgreSQL
  3. Build context string
  4. Pass to local LLM for a conversational response
"""

import re

from backend.models.openai_client import generate, AGENT_MODEL

SYSTEM_PROMPT = """You are a Riphah International University student information assistant.
You have access to live student records from the university database.
Answer clearly and concisely based ONLY on the provided student data.
Format numbers neatly. If a field is missing or null, say "not on record".
Never invent data that is not in the provided context."""


# ---------------------------------------------------------------------------
# Name / reg_no extraction
# ---------------------------------------------------------------------------

_NAME_PATTERNS = [
    r"(?:student|of|for|about|show|data of|record of|profile of|details of|info(?:rmation)? (?:of|about|on))\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)",
    r"([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)'s\s+(?:data|cgpa|gpa|semester|courses?|attendance|profile|record|marks?|result|grade)",
]

_REG_PATTERN = re.compile(r"RIU-\d{4}-[A-Z]+-\d+", re.IGNORECASE)

_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any",
    "can", "her", "was", "one", "our", "out", "day", "get", "has",
    "him", "his", "how", "its", "may", "new", "now", "old", "see",
    "two", "way", "who", "boy", "did", "let", "put", "say", "she",
    "too", "use", "what", "show", "data", "info", "record", "profile",
    "semester", "course", "courses", "cgpa", "gpa", "grade", "result",
    "attendance", "marks", "student",
}


def _extract_identifier(text: str) -> dict:
    """Return {'name': ...} or {'reg_no': ...} or {} if nothing found."""
    m = _REG_PATTERN.search(text)
    if m:
        return {"reg_no": m.group(0).upper()}

    # Patterns require proper-case — no IGNORECASE so lowercase words don't match
    for pattern in _NAME_PATTERNS:
        m = re.search(pattern, text)
        if m:
            name = m.group(1).strip()
            words = name.split()
            words = [w for w in words if w.lower() not in _STOPWORDS]
            name = " ".join(words)
            if len(name) >= 3:
                return {"name": name.title()}

    return {}


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _format_profile(profile: dict) -> str:
    if not profile:
        return ""
    s = profile.get("student", {})
    courses    = profile.get("courses", [])
    attendance = profile.get("attendance", [])

    lines = [
        f"=== STUDENT PROFILE ===",
        f"Name           : {s.get('name')}",
        f"Registration No: {s.get('reg_no')}",
        f"Department     : {s.get('department')}",
        f"Program        : {s.get('program')}",
        f"Semester       : {s.get('semester')}",
        f"CGPA           : {s.get('cgpa')}",
        f"Status         : {s.get('status', 'active').title()}",
        f"Campus         : {s.get('campus')}",
        f"Enrollment Year: {s.get('enrollment_year')}",
        f"Email          : {s.get('email')}",
        f"Phone          : {s.get('phone', 'N/A')}",
        f"Father Name    : {s.get('father_name', 'N/A')}",
        f"Address        : {s.get('address', 'N/A')}",
        "",
    ]

    if courses:
        lines.append(f"=== CURRENT SEMESTER COURSES (Semester {s.get('semester')}) ===")
        for c in courses:
            grade_str = f"Grade: {c.get('grade','—')} ({c.get('grade_points','—')} pts)" if c.get("grade") else "Ongoing"
            lines.append(
                f"  {c.get('code'):<10} {c.get('name'):<40} "
                f"{c.get('credit_hours')} cr   {grade_str}   "
                f"Instructor: {c.get('instructor','—')}"
            )
        lines.append("")

    if attendance:
        lines.append(f"=== ATTENDANCE (Semester {s.get('semester')}) ===")
        for a in attendance:
            pct = float(a.get("percentage") or 0)
            flag = " ⚠ BELOW 75%" if pct < 75 else ""
            lines.append(
                f"  {a.get('code'):<10} {a.get('name'):<40} "
                f"{a.get('attended')}/{a.get('total_classes')} classes  "
                f"({pct:.1f}%){flag}"
            )
        lines.append("")

    return "\n".join(lines)


def _format_student_list(students: list) -> str:
    if not students:
        return "No students found matching that query."
    lines = [f"Found {len(students)} student(s):\n"]
    for s in students:
        lines.append(
            f"  • {s.get('name'):<25} {s.get('reg_no'):<22} "
            f"Semester {s.get('semester')}  CGPA {s.get('cgpa')}  "
            f"{s.get('department')}  [{s.get('campus')}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query type detection
# ---------------------------------------------------------------------------

_LIST_KEYWORDS = [
    "all students", "list students", "list all", "students in",
    "students of", "show all", "how many students",
]

_DEPT_KEYWORDS = {
    "computer science": "Computer Science",
    "cs": "Computer Science",
    "artificial intelligence": "Artificial Intelligence",
    "ai": "Artificial Intelligence",
    "electrical": "Electrical Engineering",
    "ee": "Electrical Engineering",
    "business": "Business Administration",
    "mba": "Business Administration",
    "mathematics": "Mathematics",
}


def _is_list_query(text: str) -> str | None:
    """Return department name if listing query, else None."""
    lower = text.lower()
    if not any(kw in lower for kw in _LIST_KEYWORDS):
        return None
    for kw, dept in _DEPT_KEYWORDS.items():
        if kw in lower:
            return dept
    return ""  # empty string = all departments


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(user_text: str, history: list | None = None) -> dict:
    from backend.database.students_db import (
        get_student_by_name,
        get_student_by_reg,
        get_student_full_profile,
        get_all_students,
        search_students,
    )

    history = history or []
    context = ""
    response_text = ""
    sources: list = []

    # ── List all / by department ────────────────────────────────────────────
    dept = _is_list_query(user_text)
    if dept is not None:
        try:
            students = get_all_students(department=dept or None, limit=20)
            context = _format_student_list(students)
            sources = [{"title": "Student Database", "category": "structured"}]
        except Exception as e:
            context = f"Database error: {e}"

    else:
        # ── Look up specific student ────────────────────────────────────────
        identifier = _extract_identifier(user_text)

        student = None
        try:
            if "reg_no" in identifier:
                student = get_student_by_reg(identifier["reg_no"])
            elif "name" in identifier:
                student = get_student_by_name(identifier["name"])
            else:
                # No clear name — do a broad keyword search
                words = [w for w in user_text.split() if len(w) >= 4]
                for w in words:
                    results = search_students(w)
                    if results:
                        student = results[0]
                        break
        except Exception as e:
            context = f"Database error: {e}"

        if student:
            try:
                profile = get_student_full_profile(student["id"])
                context = _format_profile(profile)
                sources = [{"title": f"Student Record: {student['name']}", "category": "structured"}]
            except Exception as e:
                context = f"Error loading profile: {e}"
        elif not context:
            name_hint = identifier.get("name") or identifier.get("reg_no") or "that student"
            context = f"No student named '{name_hint}' was found in the Riphah database."

    # ── Generate LLM response ────────────────────────────────────────────────
    hist_block = ""
    for msg in (history or [])[-4:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        hist_block += f"{role}: {msg.get('content','')}\n"

    prompt = (
        f"{hist_block}"
        f"User: {user_text}\n\n"
        f"Available data:\n{context}\n\n"
        "Provide a helpful, conversational response based on the data above."
    )

    try:
        response_text = generate(AGENT_MODEL, prompt, SYSTEM_PROMPT)
    except Exception as e:
        response_text = context or f"Sorry, could not generate a response: {e}"

    return {
        "response": response_text,
        "sources":  sources,
        "raw_data": context,
    }
