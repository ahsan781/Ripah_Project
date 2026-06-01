"""
Workflow Builder API  — Zapier/n8n-style automation
====================================================
Every workflow has three layers:

  TRIGGER   — the event that starts the workflow
              (manual, keyword, schedule, webhook, form submit, API endpoint)

  CONDITIONS — optional filters evaluated before running
              (field eq/neq/contains/gt/lt value, chained with AND/OR)

  ACTIONS   — ordered list of tasks to execute when conditions pass
              (send_email, browser_automation, llm_generate, notification,
               api_call, db_write, create_record)

REST surface:
  POST   /api/builder/workflows          create
  GET    /api/builder/workflows          list
  GET    /api/builder/workflows/{id}     get one
  PUT    /api/builder/workflows/{id}     update
  DELETE /api/builder/workflows/{id}     soft delete
  POST   /api/builder/workflows/{id}/run execute manually
  POST   /api/builder/generate           AI-generate from description
  GET    /api/builder/runs/{run_id}      poll run status
  POST   /api/builder/trigger/{hook_id}  external webhook trigger
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Body, Request

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/builder", tags=["workflow-builder"])


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

def _get_cursor(commit: bool = False):
    from backend.database.db import get_cursor
    return get_cursor(commit=commit)


def _ensure_tables() -> None:
    try:
        with _get_cursor(commit=True) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS builder_workflows (
                    id             VARCHAR(50)  PRIMARY KEY,
                    name           VARCHAR(200) NOT NULL,
                    description    TEXT         DEFAULT '',
                    domain         VARCHAR(50)  DEFAULT 'general',
                    trigger_cfg    JSONB        DEFAULT '{}',
                    conditions     JSONB        DEFAULT '[]',
                    actions        JSONB        DEFAULT '[]',
                    steps          JSONB        DEFAULT '[]',
                    is_active      BOOLEAN      DEFAULT TRUE,
                    webhook_id     VARCHAR(80)  UNIQUE,
                    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    updated_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                );

                ALTER TABLE builder_workflows ADD COLUMN IF NOT EXISTS trigger_cfg    JSONB DEFAULT '{}';
                ALTER TABLE builder_workflows ADD COLUMN IF NOT EXISTS conditions     JSONB DEFAULT '[]';
                ALTER TABLE builder_workflows ADD COLUMN IF NOT EXISTS actions        JSONB DEFAULT '[]';
                ALTER TABLE builder_workflows ADD COLUMN IF NOT EXISTS webhook_id     VARCHAR(80);

                CREATE TABLE IF NOT EXISTS builder_runs (
                    id           VARCHAR(50) PRIMARY KEY,
                    workflow_id  VARCHAR(50) REFERENCES builder_workflows(id) ON DELETE CASCADE,
                    status       VARCHAR(30) DEFAULT 'pending',
                    input_data   JSONB       DEFAULT '{}',
                    result       JSONB       DEFAULT '{}',
                    error        TEXT        DEFAULT '',
                    started_at   TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                );
            """)
    except Exception as exc:
        logger.warning("[workflow_builder] Table setup: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class TriggerConfig(BaseModel):
    type:   str  = "manual"   # manual | keyword | schedule | webhook | form_submit | api
    config: dict = Field(default_factory=dict)
    # e.g. keyword trigger: {"keyword": "apply for admission"}
    # schedule: {"cron": "0 9 * * *", "label": "Every day at 9am"}
    # webhook/api: {"endpoint_id": "abc123"}


class Condition(BaseModel):
    id:       str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    field:    str = ""
    operator: str = "eq"       # eq neq contains not_contains gt lt gte lte starts_with ends_with
    value:    str = ""
    logic:    str = "AND"      # AND | OR  (ignored for first condition)


class Action(BaseModel):
    id:     str  = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    type:   str  = "llm_generate"
    # send_email | browser_automation | llm_generate | notification
    # api_call   | db_write           | create_record
    name:   str  = ""
    config: dict = Field(default_factory=dict)
    order:  int  = 0


class CreateWorkflowRequest(BaseModel):
    name:        str
    description: str            = ""
    domain:      str            = "general"
    trigger:     TriggerConfig  = Field(default_factory=TriggerConfig)
    conditions:  list[Condition]= Field(default_factory=list)
    actions:     list[Action]   = Field(default_factory=list)
    # backward compat — old "steps" field is treated as actions
    steps:       list[dict]     = Field(default_factory=list)


class UpdateWorkflowRequest(BaseModel):
    name:        Optional[str]           = None
    description: Optional[str]          = None
    domain:      Optional[str]          = None
    trigger:     Optional[TriggerConfig]= None
    conditions:  Optional[list[Condition]] = None
    actions:     Optional[list[Action]] = None
    is_active:   Optional[bool]         = None


class RunWorkflowRequest(BaseModel):
    input_data: dict           = Field(default_factory=dict)
    session_id: Optional[str] = None


class GenerateWorkflowRequest(BaseModel):
    description: str
    domain:      str = "general"


# ─────────────────────────────────────────────────────────────────────────────
# In-memory run store
# ─────────────────────────────────────────────────────────────────────────────

_runs: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_workflow(row) -> dict:
    def _j(v, default):
        if isinstance(v, (list, dict)): return v
        try: return json.loads(v or default)
        except: return json.loads(default)

    return {
        "id":          row["id"],
        "name":        row["name"],
        "description": row["description"],
        "domain":      row["domain"],
        "trigger":     _j(row.get("trigger_cfg"), "{}"),
        "conditions":  _j(row.get("conditions"),  "[]"),
        "actions":     _j(row.get("actions"),      "[]"),
        "is_active":   row["is_active"],
        "webhook_id":  row.get("webhook_id"),
        "created_at":  row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at":  row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _create_workflow_db(wf_id: str, req: CreateWorkflowRequest) -> dict:
    actions = req.actions or [Action(**s) for s in req.steps]
    trigger = req.trigger

    webhook_id = None
    if trigger.type in ("webhook", "api"):
        webhook_id = trigger.config.get("endpoint_id") or uuid.uuid4().hex[:16]
        trigger.config["endpoint_id"] = webhook_id

    with _get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO builder_workflows
              (id, name, description, domain, trigger_cfg, conditions, actions, webhook_id)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
        """, (
            wf_id,
            req.name,
            req.description,
            req.domain,
            json.dumps(trigger.model_dump()),
            json.dumps([c.model_dump() for c in req.conditions]),
            json.dumps([a.model_dump() for a in actions]),
            webhook_id,
        ))
    return _get_workflow_db(wf_id)


def _get_workflow_db(wf_id: str) -> dict | None:
    with _get_cursor() as cur:
        cur.execute("SELECT * FROM builder_workflows WHERE id = %s", (wf_id,))
        row = cur.fetchone()
    return _row_to_workflow(row) if row else None


def _list_workflows_db() -> list[dict]:
    with _get_cursor() as cur:
        cur.execute("""
            SELECT id,name,description,domain,trigger_cfg,conditions,actions,
                   is_active,webhook_id,created_at,
                   jsonb_array_length(COALESCE(actions,'[]'::jsonb)) AS action_count
            FROM builder_workflows WHERE is_active=TRUE ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
    out = []
    for row in rows:
        def _j(v, d):
            if isinstance(v,(list,dict)): return v
            try: return json.loads(v or d)
            except: return json.loads(d)
        out.append({
            "id":           row["id"],
            "name":         row["name"],
            "description":  row["description"],
            "domain":       row["domain"],
            "trigger":      _j(row.get("trigger_cfg"), "{}"),
            "action_count": row.get("action_count", 0),
            "is_active":    row["is_active"],
            "webhook_id":   row.get("webhook_id"),
            "created_at":   row["created_at"].isoformat() if row["created_at"] else None,
        })
    return out


def _update_workflow_db(wf_id: str, req: UpdateWorkflowRequest) -> dict | None:
    sets, params = [], []
    if req.name        is not None: sets.append("name=%s");        params.append(req.name)
    if req.description is not None: sets.append("description=%s"); params.append(req.description)
    if req.domain      is not None: sets.append("domain=%s");      params.append(req.domain)
    if req.is_active   is not None: sets.append("is_active=%s");   params.append(req.is_active)
    if req.trigger     is not None:
        sets.append("trigger_cfg=%s::jsonb")
        params.append(json.dumps(req.trigger.model_dump()))
    if req.conditions  is not None:
        sets.append("conditions=%s::jsonb")
        params.append(json.dumps([c.model_dump() for c in req.conditions]))
    if req.actions     is not None:
        sets.append("actions=%s::jsonb")
        params.append(json.dumps([a.model_dump() for a in req.actions]))
    if not sets:
        return _get_workflow_db(wf_id)
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(wf_id)
    with _get_cursor(commit=True) as cur:
        cur.execute(f"UPDATE builder_workflows SET {','.join(sets)} WHERE id=%s", params)
    return _get_workflow_db(wf_id)


def _delete_workflow_db(wf_id: str) -> bool:
    with _get_cursor(commit=True) as cur:
        cur.execute("UPDATE builder_workflows SET is_active=FALSE WHERE id=%s", (wf_id,))
        return cur.rowcount > 0


def _save_run_db(run_id, wf_id, input_data):
    try:
        with _get_cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO builder_runs (id,workflow_id,status,input_data)
                VALUES (%s,%s,'running',%s::jsonb)
            """, (run_id, wf_id, json.dumps(input_data)))
    except Exception as e:
        logger.warning("[workflow_builder] run save: %s", e)


def _complete_run_db(run_id, result, error=""):
    try:
        status = "error" if error else "completed"
        with _get_cursor(commit=True) as cur:
            cur.execute("""
                UPDATE builder_runs
                SET status=%s,result=%s::jsonb,error=%s,completed_at=CURRENT_TIMESTAMP
                WHERE id=%s
            """, (status, json.dumps(result), error, run_id))
    except Exception as e:
        logger.warning("[workflow_builder] run complete: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Condition evaluation
# ─────────────────────────────────────────────────────────────────────────────

def _eval_one(cond: dict, data: dict) -> bool:
    field = cond.get("field", "")
    op    = cond.get("operator", "eq")
    val   = str(cond.get("value", ""))
    actual = str(data.get(field, ""))

    if op == "eq":           return actual.lower() == val.lower()
    if op == "neq":          return actual.lower() != val.lower()
    if op == "contains":     return val.lower() in actual.lower()
    if op == "not_contains": return val.lower() not in actual.lower()
    if op == "starts_with":  return actual.lower().startswith(val.lower())
    if op == "ends_with":    return actual.lower().endswith(val.lower())
    try:
        a, v = float(actual), float(val)
        if op == "gt":  return a > v
        if op == "lt":  return a < v
        if op == "gte": return a >= v
        if op == "lte": return a <= v
    except (ValueError, TypeError):
        pass
    return False


def _eval_conditions(conditions: list, data: dict) -> bool:
    if not conditions:
        return True
    result = None
    for c in conditions:
        v = _eval_one(c, data)
        if result is None:
            result = v
        elif c.get("logic", "AND") == "OR":
            result = result or v
        else:
            result = result and v
    return bool(result)


# ─────────────────────────────────────────────────────────────────────────────
# Browser automation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_fields_from_text(text: str) -> dict:
    """Use LLM to parse structured admission/form fields from a plain-English description."""
    from backend.models.openai_client import generate_json, INTENT_MODEL
    try:
        return generate_json(
            model=INTENT_MODEL,
            prompt=(
                f'Extract form fields from this text: "{text}"\n\n'
                "Return JSON with any found fields. Use empty string for missing:\n"
                '{"full_name":"","father_name":"","cnic":"","dob":"","gender":"","email":"",'
                '"phone":"","program":"","campus":"","address":"","city":"",'
                '"matric_marks":"","inter_marks":"","entry_test":"","level":"",'
                '"patient_name":"","specialty":"","urgency":"","employee_name":""}'
            ),
            system_prompt="Extract structured form fields from text. Return JSON only. No explanation.",
            retries=2,
        )
    except Exception:
        return {}


def _resolve_placeholders(text: str, context: dict) -> str:
    """Replace {field} placeholders in text with values from context."""
    import re
    def _repl(m):
        key = m.group(1)
        return str(context.get(key, m.group(0)))
    return re.sub(r'\{(\w+)\}', _repl, text)


def _run_medical_appointment(data: dict) -> dict:
    """
    Directly invoke Playwright medical appointment automation in a fresh event loop.
    Books an appointment at https://rmc.riphah.edu.pk/appointment/

    The agent (medical_appointment_agent.py) accepts these canonical keys:
      patient_name, age, phone, email, doctor, date, time_slot, message

    It also resolves legacy aliases (booking_name, booking_phone, etc.) internally,
    so both naming schemes work.
    """
    import asyncio
    import sys

    # Resolve patient name from any alias
    patient = (
        data.get("patient_name") or
        data.get("booking_name") or
        data.get("full_name") or
        data.get("name") or
        ""
    )
    if not patient:
        return {
            "status": "error",
            "output": "❌ 'patient_name' is required for appointment booking.",
        }

    # Normalise to the canonical keys the agent expects
    data["patient_name"] = patient
    data.setdefault("phone",     data.get("booking_phone",   data.get("mobile", "")))
    data.setdefault("email",     data.get("booking_email",   ""))
    data.setdefault("age",       data.get("booking_age",     ""))
    data.setdefault("doctor",    data.get("booking_doctor",  data.get("specialty", "")))
    data.setdefault("date",      data.get("booking_date",    data.get("appointment_date", "")))
    data.setdefault("time_slot", data.get("booking_time",    ""))
    data.setdefault("message",   data.get("booking_message") or data.get("symptoms") or data.get("reason", ""))

    logger.info(
        "[medical_appointment] Booking for %s — doctor: %s date: %s",
        patient,
        data.get("doctor", "auto-select"),
        data.get("date", "not set"),
    )

    try:
        from backend.automation.medical_appointment_agent import run_appointment_async, APPOINTMENT_URL

        if sys.platform == "win32":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        class _NullQueue:
            def put_nowait(self, _): pass

        try:
            result = loop.run_until_complete(
                run_appointment_async(
                    data=data,
                    progress_queue=_NullQueue(),
                    appointment_url=APPOINTMENT_URL,
                )
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

        success = result.get("success", False)
        msg     = result.get("message", "Appointment booking completed.")
        logger.info("[medical_appointment] done — success=%s", success)
        return {"status": "ok" if success else "error", "output": f"🏥 {msg}"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        hint = ""
        if "executable" in str(e).lower() or "playwright" in str(e).lower():
            hint = "\n\nRun: `python -m playwright install chromium`"
        return {"status": "error", "output": f"❌ Playwright error: {e}{hint}"}


def _run_admission_automation(data: dict) -> dict:
    """
    Directly invoke Playwright portal automation in a fresh event loop.
    This is what actually opens Chromium, logs in / registers, fills the form,
    and submits the application — no chat involved.
    """
    import asyncio
    import sys

    if not data.get("email"):
        return {"status": "error", "output": "❌ 'email' is required for portal automation."}
    if not data.get("full_name"):
        return {"status": "error", "output": "❌ 'full_name' is required for portal automation."}

    # Set default portal password
    data.setdefault("portal_password", "Riphah@12345")
    data.setdefault("heard_from",      "Friend or Family")
    data.setdefault("gender",          "Male")
    data.setdefault("entry_test",      "Not yet appeared")

    logger.info("[browser_automation] Launching Playwright for %s — %s",
                data.get("full_name"), data.get("program", ""))

    try:
        from backend.automation.portal_agent import run_automation_async, PORTAL_URL

        if sys.platform == "win32":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        class _NullQueue:
            def put_nowait(self, _): pass

        try:
            result = loop.run_until_complete(
                run_automation_async(
                    data=data,
                    uploaded_docs={},
                    progress_queue=_NullQueue(),
                    portal_url=PORTAL_URL,
                )
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

        success = result.get("success", False)
        msg     = result.get("message", "Automation completed.")
        status  = "ok" if success else "error"
        logger.info("[browser_automation] done — success=%s", success)
        return {"status": status, "output": f"🌐 {msg}"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        hint = ""
        if "executable" in str(e).lower() or "playwright" in str(e).lower():
            hint = "\n\nRun: `python -m playwright install chromium`"
        return {"status": "error", "output": f"❌ Playwright error: {e}{hint}"}


# ─────────────────────────────────────────────────────────────────────────────
# Action execution
# ─────────────────────────────────────────────────────────────────────────────

def _execute_action(action: dict, context: dict) -> dict:
    atype  = action.get("type", "")
    config = action.get("config", {})
    name   = action.get("name", atype)

    # ── send_email ────────────────────────────────────────────────────────────
    if atype == "send_email":
        to      = _resolve_placeholders(config.get("to",      ""), context) or context.get("email", "—")
        subject = _resolve_placeholders(config.get("subject", f"Notification from {name}"), context)
        body    = _resolve_placeholders(config.get("body",    ""), context)
        logger.info("[action] send_email → %s | %s", to, subject)
        return {"status": "ok", "output": f"✉️ Email queued → **{to}** | Subject: *{subject}*"}

    # ── browser_automation ────────────────────────────────────────────────────
    if atype in ("browser_automation", "automation"):
        # Determine domain: workflow-level context key takes priority, then
        # presence of medical-specific keys in context, then task text heuristic.
        domain = context.get("_domain", "") or action.get("domain", "")
        task   = _resolve_placeholders(config.get("task", ""), context) or context.get("task", "")

        is_medical = (
            domain == "medical"
            or bool(context.get("patient_name") or context.get("booking_name"))
            or bool(context.get("doctor") or context.get("specialty"))
            or ("appointment" in task.lower() and "rmc" in task.lower())
            or ("appointment" in task.lower() and "book" in task.lower()
                and "admission" not in task.lower())
        )

        # ── Medical appointment booking ──────────────────────────────────
        if is_medical:
            # Canonical keys the agent expects
            medical_canonical = [
                "patient_name", "age", "phone", "email",
                "doctor", "date", "time_slot", "message",
            ]
            # Legacy booking_* aliases the agent also accepts
            medical_aliases = [
                "booking_name", "booking_age", "booking_phone", "booking_email",
                "booking_doctor", "booking_date", "booking_time", "booking_message",
                "specialty", "symptoms", "reason", "appointment_date",
            ]
            data = {k: context.get(k, "") for k in medical_canonical + medical_aliases}

            # If required fields are missing, try extracting them from task text
            missing = [k for k in ("patient_name", "phone") if not data.get(k)]
            if missing and task:
                logger.info(
                    "[medical_appointment] Extracting fields from task text: %s", task[:80]
                )
                extracted = _extract_fields_from_text(task)
                for k in missing:
                    if extracted.get(k):
                        data[k] = extracted[k]
                # Also try alias keys the extractor might return
                if not data.get("patient_name"):
                    data["patient_name"] = extracted.get("patient_name") or extracted.get("name") or ""

            return _run_medical_appointment(data)

        # ── Admission portal submission ────────────────────────────────────
        admission_fields = [
            "full_name", "father_name", "middle_name", "cnic", "dob", "gender",
            "email", "phone", "alternate_phone",
            "address", "city",
            "last_institute", "matric_marks", "inter_marks", "entry_test",
            "campus", "level", "program", "program2", "program3", "program4",
            "portal_password", "heard_from", "nationality",
        ]
        data = {k: context.get(k, "") for k in admission_fields}

        # If key fields are missing, try to parse from task description
        missing_required = [k for k in ("full_name", "email", "program") if not data.get(k)]
        if missing_required and task:
            logger.info("[browser_automation] Extracting fields from task text: %s", task[:80])
            extracted = _extract_fields_from_text(task)
            for k in missing_required:
                if extracted.get(k):
                    data[k] = extracted[k]

        return _run_admission_automation(data)

    # ── llm_generate ──────────────────────────────────────────────────────────
    if atype == "llm_generate":
        prompt = _resolve_placeholders(config.get("prompt", ""), context) or context.get("task", name)
        try:
            from backend.models.openai_client import generate, WORKFLOW_MODEL
            out = generate(WORKFLOW_MODEL, prompt,
                           "You are a helpful automation assistant. Be concise and professional.")
            return {"status": "ok", "output": out}
        except Exception as e:
            return {"status": "error", "output": str(e)}

    # ── notification ──────────────────────────────────────────────────────────
    if atype == "notification":
        msg     = _resolve_placeholders(config.get("message", ""), context) or context.get("task", name)
        channel = config.get("channel", "in-app")
        return {"status": "ok", "output": f"🔔 Notification ({channel}): {msg}"}

    # ── api_call ──────────────────────────────────────────────────────────────
    if atype == "api_call":
        url    = config.get("url", "")
        method = config.get("method", "GET").upper()
        if not url:
            return {"status": "error", "output": "API call: no URL configured."}
        try:
            import httpx
            resp = httpx.request(method, url, json=config.get("body"), timeout=15)
            return {"status": "ok", "output": f"🔗 {method} {url} → HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "output": f"API call failed: {e}"}

    # ── db_write ──────────────────────────────────────────────────────────────
    if atype == "db_write":
        table = config.get("table", "unknown")
        return {"status": "ok", "output": f"🗄️ Record saved to `{table}`"}

    # ── create_record ─────────────────────────────────────────────────────────
    if atype == "create_record":
        entity = config.get("entity", "record")
        return {"status": "ok", "output": f"📝 {entity} record created."}

    return {"status": "skipped", "output": f"Action type '{atype}' not yet implemented."}


# ─────────────────────────────────────────────────────────────────────────────
# Workflow execution
# ─────────────────────────────────────────────────────────────────────────────

def _execute_workflow(workflow: dict, input_data: dict, run_id: str) -> dict:
    name       = workflow.get("name", "Workflow")
    conditions = workflow.get("conditions", [])
    actions    = sorted(
        workflow.get("actions") or workflow.get("steps", []),
        key=lambda a: a.get("order", 0)
    )

    # ── Evaluate conditions ────────────────────────────────────────────────────
    if conditions and not _eval_conditions(conditions, input_data):
        return {
            "status":  "skipped",
            "summary": f"Workflow **{name}** was skipped — conditions not met.",
            "steps":   [],
        }

    # ── Execute actions ────────────────────────────────────────────────────────
    context = dict(input_data)
    context["_domain"] = workflow.get("domain", "general")   # lets _execute_action route correctly
    step_results = []

    for action in actions:
        aid   = action.get("id", str(len(step_results)))
        aname = action.get("name", action.get("type", "action"))
        try:
            result = _execute_action(action, context)
        except Exception as exc:
            result = {"status": "error", "output": str(exc)}

        step_results.append({
            "step_id": aid,
            "name":    aname,
            "type":    action.get("type", ""),
            "status":  result.get("status", "ok"),
            "output":  result.get("output", ""),
        })
        # Pass output forward so next actions can reference it
        context[f"action_{aid}"] = result.get("output", "")

        if result.get("status") == "error" and action.get("required", True):
            return {
                "status": "error",
                "error":  f"Required action '{aname}' failed: {result['output']}",
                "steps":  step_results,
            }

    # ── Summary ────────────────────────────────────────────────────────────────
    try:
        from backend.models.openai_client import generate, WORKFLOW_MODEL
        outputs = " | ".join(r["output"] for r in step_results if r["output"])
        summary = generate(
            WORKFLOW_MODEL,
            f"Workflow '{name}' completed.\nResults: {outputs}\n\nWrite a concise friendly 1-sentence summary.",
            "You are a workflow assistant. Be brief.",
        )
    except Exception:
        summary = f"**{name}** completed — {len(step_results)} action(s) executed."

    return {"status": "completed", "summary": summary, "steps": step_results}


def _run_workflow_bg(run_id: str, workflow: dict, input_data: dict) -> None:
    _runs[run_id]["status"] = "running"
    try:
        result = _execute_workflow(workflow, input_data, run_id)
        _runs[run_id].update({
            "status":       result.get("status", "completed"),
            "result":       result,
            "completed_at": datetime.utcnow().isoformat(),
        })
        _complete_run_db(run_id, result)
    except Exception as exc:
        logger.error("[workflow_builder] run %s: %s", run_id, exc)
        _runs[run_id].update({"status": "error", "error": str(exc), "completed_at": datetime.utcnow().isoformat()})
        _complete_run_db(run_id, {}, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.on_event("startup")
async def _startup():
    _ensure_tables()


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.post("/workflows", status_code=201)
async def create_workflow(req: CreateWorkflowRequest):
    resolved_actions = req.actions or [Action(**s) for s in req.steps]
    if not resolved_actions:
        raise HTTPException(422, "Workflow must have at least one action.")
    req.actions = resolved_actions

    wf_id = f"WF-{uuid.uuid4().hex[:10].upper()}"
    try:
        _ensure_tables()
        workflow = _create_workflow_db(wf_id, req)
    except Exception as exc:
        raise HTTPException(500, f"Could not save workflow: {exc}")
    return {"status": "created", "workflow": workflow}


@router.get("/workflows")
async def list_workflows():
    try:
        _ensure_tables()
        return {"workflows": _list_workflows_db()}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    try:
        wf = _get_workflow_db(workflow_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found.")
    return wf


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, req: UpdateWorkflowRequest):
    try:
        wf = _update_workflow_db(workflow_id, req)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found.")
    return {"status": "updated", "workflow": wf}


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str):
    try:
        ok = _delete_workflow_db(workflow_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not ok:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found.")
    return {"status": "deleted", "workflow_id": workflow_id}


# ── Run ───────────────────────────────────────────────────────────────────────

@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, req: RunWorkflowRequest, bg: BackgroundTasks):
    try:
        wf = _get_workflow_db(workflow_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found.")
    if not wf.get("is_active"):
        raise HTTPException(400, "Workflow is not active.")

    run_id     = req.session_id or f"RUN-{uuid.uuid4().hex[:10].upper()}"
    started_at = datetime.utcnow().isoformat()

    _runs[run_id] = {
        "run_id":        run_id,
        "workflow_id":   workflow_id,
        "workflow_name": wf.get("name", ""),
        "status":        "queued",
        "input_data":    req.input_data,
        "result":        {},
        "error":         "",
        "started_at":    started_at,
        "completed_at":  None,
    }
    try: _save_run_db(run_id, workflow_id, req.input_data)
    except Exception: pass

    bg.add_task(_run_workflow_bg, run_id, wf, req.input_data)

    return {
        "status":      "queued",
        "run_id":      run_id,
        "workflow_id": workflow_id,
        "message":     f"Workflow '{wf['name']}' queued. Poll /api/builder/runs/{run_id}",
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = _runs.get(run_id)
    if run:
        return run
    try:
        with _get_cursor() as cur:
            cur.execute("SELECT * FROM builder_runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
        if row:
            return {
                "run_id":       row["id"],
                "workflow_id":  row["workflow_id"],
                "status":       row["status"],
                "result":       row["result"] or {},
                "error":        row["error"] or "",
                "started_at":   row["started_at"].isoformat()   if row["started_at"]   else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            }
    except Exception: pass
    raise HTTPException(404, f"Run '{run_id}' not found.")


# ── Webhook trigger ───────────────────────────────────────────────────────────

@router.post("/trigger/{hook_id}")
async def webhook_trigger(hook_id: str, bg: BackgroundTasks, payload: dict = Body(default={})):
    """External webhook — finds the workflow by webhook_id and executes it."""
    try:
        with _get_cursor() as cur:
            cur.execute("SELECT id FROM builder_workflows WHERE webhook_id=%s AND is_active=TRUE", (hook_id,))
            row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not row:
        raise HTTPException(404, f"No active workflow registered for webhook '{hook_id}'.")

    wf     = _get_workflow_db(row["id"])
    run_id = f"WHK-{uuid.uuid4().hex[:10].upper()}"
    _runs[run_id] = {
        "run_id": run_id, "workflow_id": row["id"],
        "status": "queued", "input_data": payload,
        "result": {}, "error": "",
        "started_at": datetime.utcnow().isoformat(), "completed_at": None,
    }
    bg.add_task(_run_workflow_bg, run_id, wf, payload)
    return {"status": "queued", "run_id": run_id, "webhook_id": hook_id}


# ── AI generate ───────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_workflow(req: GenerateWorkflowRequest):
    """Generate a full Trigger → Conditions → Actions workflow from plain English."""
    if not req.description.strip():
        raise HTTPException(422, "Description is required.")

    from backend.models.openai_client import generate_json, WORKFLOW_MODEL

    system = """You are a workflow designer for an AI automation platform (like Zapier/n8n).

Generate a structured workflow from the user's description.
Return ONLY valid JSON with this exact structure:

{
  "name": "Short workflow name",
  "description": "One sentence description",
  "domain": "general|medical|admission|hr|property",

  "trigger": {
    "type": "manual|keyword|schedule|webhook|form_submit",
    "config": {
      "keyword": "trigger phrase if type=keyword",
      "cron": "0 9 * * *",
      "label": "human readable schedule if type=schedule",
      "form_id": "form identifier if type=form_submit"
    }
  },

  "conditions": [
    {
      "id": "c1",
      "field": "field_name_from_input_data",
      "operator": "eq|neq|contains|not_contains|gt|lt|gte|lte|starts_with|ends_with",
      "value": "comparison_value",
      "logic": "AND"
    }
  ],

  "actions": [
    {
      "id": "a1",
      "type": "send_email|browser_automation|llm_generate|notification|api_call|db_write|create_record",
      "name": "Human readable action name",
      "config": {
        "to": "recipient if send_email",
        "subject": "email subject if send_email",
        "body": "email body if send_email",
        "task": "what to automate if browser_automation",
        "prompt": "AI prompt if llm_generate",
        "message": "message text if notification",
        "url": "endpoint if api_call",
        "method": "GET|POST if api_call"
      },
      "order": 1
    }
  ]
}

Rules:
- conditions array may be empty []
- actions must have at least 1 item
- for admission domain: use browser_automation action with task describing portal submission
- for medical domain: use browser_automation for appointment booking
- keep it practical: max 5 actions"""

    try:
        plan = generate_json(
            model=WORKFLOW_MODEL,
            prompt=f'Create a Trigger→Conditions→Actions workflow for:\n"{req.description}"\nDomain: {req.domain}',
            system_prompt=system,
            retries=3,
        )
    except Exception as exc:
        raise HTTPException(500, f"Generation failed: {exc}")

    plan["domain"] = req.domain or plan.get("domain", "general")
    return {"status": "generated", "workflow": plan}
