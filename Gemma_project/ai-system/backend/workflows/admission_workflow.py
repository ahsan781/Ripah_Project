"""
AI-driven admission workflow for Riphah International University.

Phases:
  scanning   — portal is being explored; first user message triggers exploration
  collecting — gathering applicant information conversationally
  review     — all info collected, user reviews and can edit any field
  editing    — user is editing a specific field
  confirm    — final yes/no before portal automation starts
  automating — Playwright automation is running in background
  complete   — automation finished (success or failure)
  cancelled  — user cancelled

Exploration order:
  1. Open the portal homepage
  2. Group form elements by parent <form> to separate login / registration
  3. Discover CSS selectors for each form field
  4. Store schema in workflow cache (3-day TTL) for instant reuse

The module is stateless: all state lives in the dict returned by start().
main.py stores this dict in _session_workflows[session_id] and passes
it back on every advance() call.
"""

import re
import uuid
from datetime import datetime
from typing import Optional, Tuple

from backend.automation.portal_explorer import (
    DEFAULT_DATA_FIELDS as DEFAULT_FIELDS,
    PORTAL_URL,
    explore_portal_sync,
)


CANCEL_KEYWORDS = {"cancel", "stop", "quit", "abort", "exit", "nevermind", "never mind"}

# Minimum strength for a user-chosen portal password
MIN_PASSWORD_LEN = 8

# After this many consecutive failed attempts on the same field, show a detailed example
MAX_FIELD_RETRIES = 2

# Keywords that indicate the user is off-topic during form collection
_OFFTOPIC_SIGNALS = re.compile(
    r"\b(weather|news|joke|hello|hi |hey |what is|who is|when is|how are|tell me about"
    r"|explain|define|what does|stock|price|btc|cricket|football|movie|song)\b",
    re.I,
)


def _derive_default_password(cnic: str) -> str:
    """Simple memorable default — user should override this during review."""
    return "Riphah@12345"


def _is_offtopic(text: str, field: dict) -> bool:
    """Return True if the input looks like an off-topic question, not a field answer."""
    stripped = text.strip()
    # Very long text with a question mark is likely off-topic
    if len(stripped) > 80 and stripped.endswith("?"):
        return True
    # Matches obvious off-topic signals
    if _OFFTOPIC_SIGNALS.search(stripped):
        return True
    return False


def _field_example(field: dict) -> str:
    """Return a concrete example value for a field to guide the user after retries."""
    examples = {
        "full_name":    "e.g. **Muhammad Ahsan Khan**",
        "father_name":  "e.g. **Muhammad Imran Khan**",
        "cnic":         "e.g. **35202-1234567-9** (13 digits with dashes)",
        "dob":          "e.g. **15/06/2000** (DD/MM/YYYY)",
        "gender":       "Type **Male**, **Female**, or **Other**",
        "email":        "e.g. **ahsan.khan@gmail.com**",
        "phone":        "e.g. **0300-1234567** or **+923001234567**",
        "program":      "e.g. **MBBS**, **BS CS**, **BBA**, **BE**, **Pharm-D**",
        "campus":       "e.g. **Islamabad**, **Lahore**, **Rawalpindi**, **Faisalabad**",
        "address":      "e.g. **House 5, Street 3, G-11/2, Islamabad**",
        "entry_test":   "e.g. **85** (your score) or type **Not yet appeared**",
        "portal_password": "e.g. **Riphah@2026** (at least 8 characters)",
    }
    return examples.get(field.get("key", ""), "")

ADMISSION_TRIGGER_KEYWORDS = [
    "apply for admission",
    "admission application",
    "apply admission",
    "fill admission form",
    "apply for mbbs",
    "apply for bds",
    "apply for pharm",
    "apply for bs",
    "apply for be",
    "apply for bba",
    "apply for mba",
    "want to apply",
    "start application",
    "begin application",
    "admission form",
    "register for admission",
    "apply to riphah",
    "apply online",
    "submit my admission",
]


# ---------------------------------------------------------------------------
# State initializer
# ---------------------------------------------------------------------------

def start(session_id: str) -> dict:
    """Create a fresh admission workflow state in the 'scanning' phase."""
    return {
        "session_id":        session_id,
        "workflow_type":     "admission",
        "phase":             "scanning",
        "fields":            [],
        "required_docs":     [],
        "collected_data":    {},
        "current_field_idx": 0,
        "field_retry_counts": {},   # key → consecutive failure count for that field
        "application_id":    f"RIU-{uuid.uuid4().hex[:8].upper()}",
        "docs_uploaded":     {},
        "automation_result": None,
        "automation_error":  None,  # last automation error message
        "workflow_schema":   {},
        "started_at":        datetime.now().isoformat(),
    }


def get_intro_message() -> str:
    return (
        "I'll help you apply to **Riphah International University**!\n\n"
        "**Step 1 — Exploring the portal** 🔍\n"
        "I'm opening the official Riphah admission portal right now to:\n"
        "• Discover the exact registration and login form fields\n"
        "• Map CSS selectors so the form can be filled automatically\n"
        "• Detect whether email / OTP verification is needed\n\n"
        "This takes about 15–35 seconds on the first run "
        "(subsequent sessions use a cached schema and start instantly).\n\n"
        "Please type **'start'** when you're ready, or just wait — "
        "I'll ask your first question as soon as the exploration completes."
    )


# ---------------------------------------------------------------------------
# Core advance function
# ---------------------------------------------------------------------------

def advance(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """
    Process one user turn.

    Returns:
        (updated_state, response_message, is_terminal)

    is_terminal=True when the workflow is done/cancelled.
    'automating' phase is NOT terminal — main.py keeps the state until the
    background task finishes.
    """
    if _is_cancel(user_input):
        state["phase"] = "cancelled"
        return state, (
            "Admission application cancelled. No data has been submitted.\n\n"
            "You can start again anytime by saying **'apply for admission'**."
        ), True

    phase = state.get("phase", "scanning")

    if phase == "scanning":
        return _handle_scanning(state)

    if phase == "collecting":
        return _handle_collecting(state, user_input)

    if phase == "review":
        return _handle_review(state, user_input)

    if phase == "editing":
        return _handle_editing(state, user_input)

    if phase == "confirm":
        return _handle_confirm(state, user_input)

    if phase == "automating":
        # While automation runs, remind user what data was collected
        data = state.get("collected_data", {})
        name = data.get("full_name", "")
        prog = data.get("program", "")
        return state, (
            f"⏳ **Submitting your application on the Riphah portal right now.**\n\n"
            f"Applicant: **{name}** | Program: **{prog}**\n\n"
            "You can see live progress in the panel below. This usually takes 1–3 minutes. Please wait..."
        ), False

    if phase == "complete":
        result = state.get("automation_result", {})
        msg    = result.get("message", "Application process complete.")
        return state, msg, True

    if phase == "error":
        return _handle_error_phase(state, user_input)

    return state, (
        "I'm not sure how to handle that.\n\n"
        "Type **'cancel'** to start over, or describe your issue and I'll try to help."
    ), False


# ---------------------------------------------------------------------------
# Phase handlers
# ---------------------------------------------------------------------------

def _handle_error_phase(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """
    Handle user messages when automation has errored out.
    Offers retry or cancel instead of silently dying.
    """
    lower = user_input.strip().lower()
    err   = state.get("automation_error") or state.get("automation_result", {}).get("message", "An error occurred.")
    data  = state.get("collected_data", {})

    retry_words  = {"retry", "try again", "try", "resubmit", "restart automation", "submit again"}
    cancel_words = {"cancel", "stop", "quit", "no", "start over", "new application"}

    if any(w in lower for w in retry_words):
        # Reset to confirm phase so automation restarts
        state["phase"] = "confirm"
        state["automation_error"] = None
        return state, (
            "No problem — let's try again.\n\n"
            + _confirm_prompt(state)
        ), False

    if any(w in lower for w in cancel_words):
        state["phase"] = "cancelled"
        return state, (
            "Application cancelled. All your data has been cleared.\n\n"
            "Say **'apply for admission'** anytime to start a fresh application."
        ), True

    # Default: show error with options
    name = data.get("full_name", "")
    prog = data.get("program", "")
    return state, (
        f"❌ **Automation error** for {name} ({prog}):\n\n"
        f"> {err}\n\n"
        "**What would you like to do?**\n"
        "• Type **'retry'** to attempt portal submission again\n"
        "• Type **'cancel'** to start a fresh application\n"
        "• Contact **admissions@riphah.edu.pk** if the problem persists"
    ), False


def _collected_summary(state: dict) -> str:
    """Return a compact one-line summary of what has been collected so far."""
    data   = state.get("collected_data", {})
    fields = state.get("fields", [])
    done   = [f for f in fields if data.get(f["key"])]
    if not done:
        return ""
    items = ", ".join(f["label"] for f in done[:4])
    extra = f" +{len(done) - 4} more" if len(done) > 4 else ""
    return f"*Collected so far: {items}{extra}*"


def _handle_scanning(state: dict) -> Tuple[dict, str, bool]:
    """
    Deep-explore the portal (or load cached schema), then ask the first question.

    The explorer:
      • Groups DOM inputs by parent <form> so login and registration are separated
      • Discovers real CSS selectors for both forms
      • Detects whether email / OTP verification is required after signup
      • Caches the result for 3 days so the next session loads instantly
    """
    schema = explore_portal_sync(PORTAL_URL, timeout_seconds=35)

    fields        = schema.get("data_fields") or DEFAULT_FIELDS
    required_docs = schema.get("required_documents", [])
    scan_failed   = schema.get("scan_failed", False)

    # Store lightweight workflow info in state (used by automation phase)
    state["workflow_schema"] = {
        "login_form":   schema.get("login_form", {}),
        "registration": schema.get("registration", {}),
        "verification": schema.get("verification", {}),
    }
    state["fields"]             = fields
    state["required_docs"]      = required_docs
    state["current_field_idx"]  = 0
    state["phase"]              = "collecting"

    total   = len(fields)
    doc_str = ", ".join(required_docs) if required_docs else "standard admission documents"

    # Build a human-readable discovery summary
    reg_keys   = list(schema.get("registration", {}).get("field_map", {}).keys())
    login_ok   = bool(schema.get("login_form", {}).get("email_sel"))
    verify_type = schema.get("verification", {}).get("type", "unknown")

    if scan_failed:
        scan_note = (
            "⚠ The portal was unreachable — using standard Riphah admission fields.\n"
        )
        discovery = ""
    else:
        scan_note = "✓ Portal explored successfully.\n"
        parts = []
        if login_ok:
            parts.append("login form selectors discovered")
        if reg_keys:
            parts.append(f"registration fields mapped: {', '.join(reg_keys)}")
        if verify_type not in ("unknown", "none"):
            parts.append(f"verification required: {verify_type}")
        elif verify_type == "none":
            parts.append("no email verification required")
        discovery = ("**Discovered:** " + " • ".join(parts) + "\n") if parts else ""

    first_q = _question(fields[0], 1, total)
    return state, (
        f"{scan_note}"
        f"{discovery}\n"
        f"I need **{total} pieces of information** from you.\n"
        f"Required documents: {doc_str}\n\n"
        f"---\n\n{first_q}"
    ), False


def _handle_collecting(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """
    Validate current field answer, store it, advance to next question.

    Improvements over original:
      - Off-topic detection: redirects back to the current question
      - 'back' command: re-answer the previous field
      - Retry counter: after MAX_FIELD_RETRIES failures, show a concrete example
      - Context summary: shows progress bar and collected fields on every response
    """
    fields = state.setdefault("fields", [])
    idx    = state.get("current_field_idx", 0)
    retries: dict = state.setdefault("field_retry_counts", {})

    if idx >= len(fields):
        _ensure_portal_password(state)
        state["phase"] = "review"
        return state, _review_prompt(state), False

    field      = fields[idx]
    field_key  = field.get("key", "")
    value      = user_input.strip()
    total      = len(fields)

    # ── 'back' command: re-answer the previous field ─────────────────────────
    if value.lower() in ("back", "go back", "previous", "prev") and idx > 0:
        state["current_field_idx"] = idx - 1
        prev_field = fields[idx - 1]
        prev_key   = prev_field.get("key", "")
        old_value  = state.get("collected_data", {}).get(prev_key, "")
        retries.pop(prev_key, None)
        summary = _collected_summary(state)
        return state, (
            f"Going back to **{prev_field['label']}**.\n"
            f"Current value: *{old_value}*\n\n"
            f"{_question(prev_field, idx, total)}"
            + (f"\n\n{summary}" if summary else "")
        ), False

    # ── Off-topic detection ───────────────────────────────────────────────────
    if _is_offtopic(value, field):
        summary = _collected_summary(state)
        return state, (
            "I'm collecting your admission details right now — I can answer general questions after we finish.\n\n"
            f"Back to the form: {_question(field, idx + 1, total)}"
            + (f"\n\n{summary}" if summary else "")
        ), False

    # ── Validate ──────────────────────────────────────────────────────────────
    error = _validate(field, value)
    if error:
        retry_count = retries.get(field_key, 0) + 1
        retries[field_key] = retry_count

        example = _field_example(field)
        hint_block = ""
        if retry_count >= MAX_FIELD_RETRIES and example:
            hint_block = f"\n\n💡 **Example:** {example}"

        summary = _collected_summary(state)
        return state, (
            f"⚠️ {error}"
            f"{hint_block}\n\n"
            f"{_question(field, idx + 1, total)}"
            + (f"\n\n{summary}" if summary else "")
        ), False

    # ── Valid: store, advance ─────────────────────────────────────────────────
    retries.pop(field_key, None)   # reset retry counter on success
    value = _normalize(field, value)
    state.setdefault("collected_data", {})[field_key] = value
    state["current_field_idx"] = idx + 1
    next_idx = state["current_field_idx"]

    # Progress bar  e.g.  [████████░░░░]  8/12
    done_pct    = int((next_idx / total) * 12)
    progress    = "█" * done_pct + "░" * (12 - done_pct)
    progress_ln = f"[{progress}] {next_idx}/{total}"

    if next_idx >= len(fields):
        _ensure_portal_password(state)
        state["phase"] = "review"
        ack = _ack(field_key, value)
        return state, f"{ack}\n\n{progress_ln} — All questions answered!\n\n{_review_prompt(state)}", False

    ack    = _ack(field_key, value)
    next_q = _question(fields[next_idx], next_idx + 1, total)
    return state, f"{ack} {progress_ln}\n\n{next_q}", False


def _ensure_portal_password(state: dict) -> None:
    """
    Ensure the workflow state carries a `portal_password` so it is
    deterministic, visible to the user during review, and reused by
    portal_agent.py instead of being silently regenerated.
    """
    data = state.setdefault("collected_data", {})
    if not data.get("portal_password"):
        data["portal_password"]            = _derive_default_password(data.get("cnic", ""))
        data.setdefault("portal_password_source", "auto")


def _handle_review(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """
    Review screen — user can edit a field or confirm.
    Commands:
      'edit <field>'         — edit a specific field
      'edit'                 — list editable fields
      'set password <pwd>'   — choose a custom portal password
      'submit' / 'yes'       — proceed to automation
      'cancel' / 'no'        — cancel
    """
    raw   = user_input.strip()
    lower = raw.lower()

    # Allow password override at the review screen
    pw_match = re.match(r"(?:set\s+)?password\s+(.+)", raw, re.IGNORECASE)
    if pw_match:
        new_pw = pw_match.group(1).strip()
        if len(new_pw) < MIN_PASSWORD_LEN:
            return state, (
                f"Password must be at least {MIN_PASSWORD_LEN} characters. "
                "Please choose a stronger one — for example:\n\n"
                "*set password MyRiphah@2026*"
            ), False
        state["collected_data"]["portal_password"]        = new_pw
        state["collected_data"]["portal_password_source"] = "user"
        return state, (
            f"Portal password updated. **{new_pw}** will be used to create your "
            f"Riphah account.\n\n" + _review_prompt(state)
        ), False

    submit_words = {"submit", "yes", "y", "confirm", "proceed", "ok", "go", "go ahead", "sure", "yep"}
    if any(w in lower for w in submit_words):
        state["phase"] = "confirm"
        return state, _confirm_prompt(state), False

    # Check for edit request
    edit_match = re.match(r"edit\s+(.*)", lower)
    if edit_match or lower == "edit":
        target = edit_match.group(1).strip() if edit_match else ""
        if not target:
            # List editable fields
            fields_list = "\n".join(
                f"• **{f['label']}** — {state['collected_data'].get(f['key'],'—')}"
                for f in state["fields"]
            )
            return state, (
                "Which field would you like to edit? Type **'edit <field name>'**, for example:\n\n"
                f"*edit full name*\n\n"
                f"**Current values:**\n{fields_list}"
            ), False

        # Find the matching field
        matched = _find_field_by_name(state["fields"], target)
        if matched:
            state["phase"] = "editing"
            state["editing_field_key"] = matched["key"]
            return state, (
                f"Editing **{matched['label']}**.\n"
                f"Current value: *{state['collected_data'].get(matched['key'], '—')}*\n\n"
                f"Please enter the new value:"
            ), False
        else:
            return state, (
                f"I couldn't find a field called '{target}'. "
                "Type **'edit'** to see all editable fields, or **'submit'** to proceed."
            ), False

    # Unknown input — re-show review
    return state, (
        "Please choose:\n"
        "• Type **'submit'** to submit your application\n"
        "• Type **'edit <field name>'** to change a value\n"
        "• Type **'cancel'** to cancel\n\n"
        + _review_prompt(state)
    ), False


def _handle_editing(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """Accept the new value for the field being edited, then return to review."""
    key = state.get("editing_field_key", "")
    field = next((f for f in state["fields"] if f["key"] == key), None)
    if not field:
        state["phase"] = "review"
        return state, _review_prompt(state), False

    value = user_input.strip()
    error = _validate(field, value)
    if error:
        return state, f"{error}\n\nPlease enter the new value for **{field['label']}**:", False

    value = _normalize(field, value)
    state["collected_data"][key] = value
    state["phase"] = "review"
    state.pop("editing_field_key", None)

    return state, (
        f"Updated **{field['label']}** to: *{value}*\n\n"
        + _review_prompt(state)
    ), False


def _handle_confirm(state: dict, user_input: str) -> Tuple[dict, str, bool]:
    """Final yes/no before kicking off portal automation."""
    lower = user_input.strip().lower()
    yes_words = {"yes", "y", "confirm", "proceed", "ok", "sure", "go", "submit", "yep", "yeah"}
    no_words  = {"no", "n", "back", "wait", "hold", "not yet", "edit"}

    if any(w in lower for w in yes_words):
        state["phase"] = "automating"
        return state, (
            "**Starting portal automation now!**\n\n"
            "I'm opening the Riphah admission portal and submitting your application.\n"
            "You'll see live progress below. This usually takes 1–3 minutes."
        ), False

    if any(w in lower for w in no_words):
        # Go back to review
        state["phase"] = "review"
        return state, (
            "No problem. Here's your information again:\n\n"
            + _review_prompt(state)
        ), False

    return state, (
        "Please type **'yes'** to confirm and submit, or **'no'** to go back and edit.\n\n"
        + _confirm_prompt(state)
    ), False


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _question(field: dict, num: int, total: int) -> str:
    label   = field.get("label", field.get("key", "Information"))
    ftype   = field.get("type", "text")
    options = field.get("options", [])

    hint = ""
    if ftype == "date":
        hint = " *(format: DD/MM/YYYY)*"
    elif ftype == "email":
        hint = " *(e.g. yourname@gmail.com)*"
    elif ftype == "phone":
        hint = " *(e.g. 0300-1234567)*"
    elif ftype == "select" and options:
        hint = f" *({' / '.join(options)})*"
    elif field["key"] == "cnic":
        hint = " *(format: 12345-1234567-1)*"
    elif field["key"] == "program":
        hint = (
            " *(e.g. MBBS, BDS, Pharm-D, DPT, BSN, BS CS, BE, BBA, MBA, LLB)*"
        )
    elif field["key"] == "campus":
        hint = " *(Islamabad / Lahore / Faisalabad / Rawalpindi / Peshawar / Karachi)*"
    elif field["key"] == "entry_test":
        hint = " *(or type 'Not yet appeared')*"
    elif ftype == "password" or field["key"] == "portal_password":
        hint = " *(min 8 characters — e.g. Riphah@12345 — save this, you'll need it to log in later)*"

    return f"**Step {num}/{total}** — {label}{hint}:"


def _validate(field: dict, value: str) -> str:
    """Return error string if invalid, empty string if valid."""
    key   = field.get("key", "")
    ftype = field.get("type", "text")

    if not value:
        return f"This field is required. Please enter your {field.get('label', key)}."

    if ftype == "email" or key == "email":
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', value):
            return "Please enter a valid email address (e.g. name@gmail.com)."

    if key == "cnic":
        clean = re.sub(r'[\s\-]', '', value)
        if not (re.match(r'^\d{13}$', clean) or re.match(r'^\d{5}-\d{7}-\d$', value)):
            return "Please enter a valid CNIC (format: 12345-1234567-1) or B-Form (13 digits)."

    if ftype == "date" or key == "dob":
        if not re.search(r'\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}', value):
            return "Please enter a valid date (format: DD/MM/YYYY)."

    if ftype == "phone" or key == "phone":
        if len(re.sub(r'[\s\-\+\(\)]', '', value)) < 10:
            return "Please enter a valid phone number (at least 10 digits)."

    if key in ("full_name", "father_name") and len(value) < 3:
        return "Name must be at least 3 characters."

    if key == "address" and len(value) < 10:
        return "Please enter your complete address (at least 10 characters)."

    if key == "portal_password" or ftype == "password":
        if len(value) < MIN_PASSWORD_LEN:
            return f"Password must be at least {MIN_PASSWORD_LEN} characters (e.g. Riphah@12345)."

    return ""


def _normalize(field: dict, value: str) -> str:
    key = field.get("key", "")
    if key == "gender":
        l = value.lower()
        if l in ("m", "male"):   return "Male"
        if l in ("f", "female"): return "Female"
        return "Other"
    return value


def _ack(key: str, value: str) -> str:
    acks = {
        "full_name":     f"Thank you, **{value}**.",
        "father_name":   "Father's name recorded.",
        "cnic":          "CNIC recorded.",
        "dob":           "Date of birth saved.",
        "gender":        f"**{value}** — noted.",
        "email":         f"Email **{value}** saved.",
        "phone":         "Phone number saved.",
        "program":       f"Applying for **{value}**.",
        "address":       "Address recorded.",
    }
    return acks.get(key, "Noted.")


def _review_prompt(state: dict) -> str:
    """Full review card shown after all info is collected."""
    data   = state["collected_data"]
    fields = state["fields"]

    rows = []
    for f in fields:
        val = data.get(f["key"], "—")
        rows.append(f"• **{f['label']}:** {val}")

    pw        = data.get("portal_password", "—")
    pw_source = data.get("portal_password_source", "auto")
    pw_note   = (
        "auto-generated from your CNIC — type **'set password <your password>'** "
        "to choose your own"
        if pw_source == "auto"
        else "chosen by you"
    )

    lines = [
        "**Please review your application information:**\n",
        *rows,
        "",
        "**Portal account that will be created on admissions.riphah.edu.pk:**",
        f"• **Login email:** {data.get('email', '—')}",
        f"• **Portal password:** `{pw}`  *({pw_note})*",
        "",
        f"**Application ID (local tracking):** {state.get('application_id', '—')}",
        "",
        "---",
        "Type **'submit'** to submit on the Riphah portal.",
        "Type **'edit <field name>'** to change any value (e.g. *edit email*).",
        "Type **'set password <your password>'** to choose a custom portal password.",
        "Type **'cancel'** to cancel.",
    ]
    return "\n".join(lines)


def _confirm_prompt(state: dict) -> str:
    """Short final confirmation before automation starts."""
    data = state["collected_data"]
    lines = [
        "**Ready to submit your application on the Riphah admission portal.**\n",
        f"• **Name:** {data.get('full_name', '—')}",
        f"• **Program:** {data.get('program', '—')}  |  **Campus:** {data.get('campus', '—')}",
        f"• **Portal email:** {data.get('email', '—')}",
        f"• **Portal password:** `{data.get('portal_password', '—')}`",
        f"• **Application ID:** {state.get('application_id', '—')}",
        "",
        "I will use this email and password to either log in (if you already have a Riphah account) "
        "or create a new account for you, then fill and submit the application form.",
        "",
        "Type **'yes'** to open the portal and submit automatically.",
        "Type **'no'** to go back and make changes.",
    ]
    return "\n".join(lines)


def _find_field_by_name(fields: list, query: str) -> Optional[dict]:
    """Find a field by partial label or key match."""
    query = query.lower().strip()
    for f in fields:
        if query in f.get("label", "").lower() or query in f.get("key", "").lower():
            return f
    return None


def _is_cancel(text: str) -> bool:
    lower = text.strip().lower()
    return lower in CANCEL_KEYWORDS or lower.startswith("cancel") or lower.startswith("abort")
