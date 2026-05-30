"""
Task Automation Workflow
========================
User describes any task in plain English.
GPT-4o generates a structured step-by-step workflow plan.
Each step is then executed by the appropriate agent.

Supported task domains:
  - medical_appointment  → appointment booking via LangGraph
  - admission            → Riphah university admission via portal agent
  - general_task         → any freeform multi-step task

Usage
-----
    from backend.workflows.task_automation_workflow import automate_task

    result = automate_task("I want to book a cardiology appointment for chest pain")
    result = automate_task("Apply for MBBS admission at Riphah for Ahsan Khan")
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from backend.models.openai_client import generate_json, generate, WORKFLOW_MODEL

logger = logging.getLogger(__name__)

# ── System prompt for workflow plan generation ─────────────────────────────────

_PLANNER_SYSTEM = """You are an intelligent workflow planner for a medical and university AI platform.

When a user describes a task, generate a structured JSON workflow plan.

Available workflow types:
  - "medical_appointment": Book a doctor appointment (specialty, urgency, patient name)
  - "admission":           Apply for university admission (Riphah International University)
  - "general_task":        Any other multi-step task

Available agents (steps can reference these):
  - "intent_parser"      : Extract intent, specialty, urgency from text
  - "auth_agent"         : Verify/look up patient or user identity
  - "schedule_agent"     : Find available appointment slots
  - "ehr_agent"          : Fetch patient medical history
  - "notify_agent"       : Send confirmation notification
  - "admission_agent"    : Collect applicant information
  - "portal_agent"       : Automate form submission on web portal
  - "document_agent"     : Handle document uploads and verification
  - "review_agent"       : Review and validate collected data

Response format (JSON only, no markdown):
{
  "workflow_type": "medical_appointment" | "admission" | "general_task",
  "task_name": "<short human-readable name>",
  "description": "<one sentence summary>",
  "estimated_steps": <number>,
  "steps": [
    {
      "step_id": 1,
      "name": "<snake_case_step_name>",
      "agent": "<agent_name>",
      "description": "<what this step does>",
      "inputs": ["<key1>", "<key2>"],
      "outputs": ["<key1>", "<key2>"],
      "required": true
    }
  ],
  "context": {
    "<any extracted info>": "<value>"
  }
}"""


# ── Workflow plan generator ────────────────────────────────────────────────────

def generate_workflow_plan(task_description: str) -> dict:
    """
    Call GPT to decompose the task into a structured step-by-step workflow.

    Returns a dict with keys: workflow_type, task_name, description,
    steps (list), context (dict).

    Raises ValueError if the LLM returns an invalid plan.
    """
    prompt = f'Task description: "{task_description}"\n\nGenerate a workflow plan for this task.'

    try:
        plan = generate_json(
            model=WORKFLOW_MODEL,
            prompt=prompt,
            system_prompt=_PLANNER_SYSTEM,
            retries=3,
        )
    except Exception as exc:
        logger.error("[task_automation] Plan generation failed: %s", exc)
        raise ValueError(f"Could not generate workflow plan: {exc}") from exc

    # Validate minimum structure
    required_keys = {"workflow_type", "task_name", "steps"}
    missing = required_keys - set(plan.keys())
    if missing:
        raise ValueError(
            f"Generated plan is missing required keys: {missing}. Got: {list(plan.keys())}"
        )

    if not isinstance(plan.get("steps"), list) or len(plan["steps"]) == 0:
        raise ValueError("Generated plan has no steps.")

    logger.info(
        "[task_automation] Plan generated — type=%s  steps=%d",
        plan["workflow_type"],
        len(plan["steps"]),
    )
    return plan


# ── Step executor ─────────────────────────────────────────────────────────────

def _execute_medical_appointment(plan: dict, task_description: str) -> dict:
    """Route to the existing LangGraph appointment workflow."""
    from backend.intent_parser import parse_intent
    from backend.workflows.appointment_workflow import run_workflow

    context = plan.get("context", {})
    patient_name = context.get("patient_name", "")

    try:
        intent = parse_intent(task_description)
        if patient_name:
            intent["patient_name"] = patient_name
    except Exception as exc:
        logger.warning("[task_automation] Intent parse failed, using context: %s", exc)
        intent = {
            "task_type": "appointment_booking",
            "specialty": context.get("specialty", "general"),
            "urgency":   context.get("urgency", "routine"),
            "symptoms":  context.get("symptoms", []),
            "patient_id": None,
        }

    try:
        result = run_workflow(intent, patient_name=patient_name)
        return {
            "status":      result.get("status", "completed"),
            "workflow_id": result.get("workflow_id"),
            "output":      result.get("ui_output", ""),
            "booking":     result.get("booking", {}),
            "slots":       result.get("available_slots", []),
            "awaiting_input": result.get("awaiting_input", ""),
        }
    except Exception as exc:
        logger.error("[task_automation] Appointment workflow error: %s", exc)
        raise RuntimeError(f"Appointment workflow failed: {exc}") from exc


def _execute_admission(plan: dict) -> dict:
    """Initialise an admission workflow session and return the intro message."""
    from backend.workflows.admission_workflow import start, get_intro_message

    session_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
    state = start(session_id)
    return {
        "status":     "collecting",
        "session_id": session_id,
        "state":      state,
        "message":    get_intro_message(),
        "next_action": "provide_applicant_details",
    }


def _execute_general_task(plan: dict, task_description: str) -> dict:
    """
    Execute a freeform multi-step task by having GPT perform each step
    and accumulate results.
    """
    _STEP_EXECUTOR_SYSTEM = (
        "You are a task execution assistant. "
        "Complete the given step clearly and concisely. "
        "Return a JSON object with keys: 'result' (string), 'status' ('ok'|'error'), "
        "'output' (main output text)."
    )

    results: list[dict] = []
    accumulated_context = {
        "task": task_description,
        "plan": plan.get("task_name", ""),
    }

    for step in plan.get("steps", []):
        step_id   = step.get("step_id", "?")
        step_name = step.get("name", "unknown")
        step_desc = step.get("description", "")
        agent     = step.get("agent", "general")

        prompt = (
            f"Task: {task_description}\n"
            f"Current step ({step_id}): {step_name}\n"
            f"Step description: {step_desc}\n"
            f"Accumulated context: {accumulated_context}\n\n"
            f"Execute this step and return the result."
        )

        try:
            step_result = generate_json(
                model=WORKFLOW_MODEL,
                prompt=prompt,
                system_prompt=_STEP_EXECUTOR_SYSTEM,
                retries=2,
            )
            step_result["step_id"]   = step_id
            step_result["step_name"] = step_name
            step_result["agent"]     = agent
            results.append(step_result)

            # Feed output into next step's context
            if step_result.get("output"):
                accumulated_context[f"step_{step_id}_output"] = step_result["output"]

            logger.info("[task_automation] Step %s/%s completed", step_id, step_name)

        except Exception as exc:
            logger.error("[task_automation] Step %s failed: %s", step_name, exc)
            results.append({
                "step_id":   step_id,
                "step_name": step_name,
                "agent":     agent,
                "status":    "error",
                "result":    str(exc),
                "output":    f"Step '{step_name}' failed: {exc}",
            })
            if step.get("required", True):
                # Required step failed — abort
                return {
                    "status":  "error",
                    "error":   f"Required step '{step_name}' failed: {exc}",
                    "results": results,
                }

    # Build a final summary
    try:
        outputs = " | ".join(
            r.get("output", "") for r in results if r.get("output")
        )
        summary = generate(
            model=WORKFLOW_MODEL,
            prompt=(
                f"Task: {task_description}\n"
                f"Workflow '{plan.get('task_name')}' completed.\n"
                f"Step outputs: {outputs}\n\n"
                "Write a single concise summary paragraph of what was accomplished."
            ),
            system_prompt="You are a task summarizer. Write a friendly, professional summary.",
        )
    except Exception as exc:
        logger.warning("[task_automation] Summary generation failed: %s", exc)
        summary = f"Task '{plan.get('task_name')}' completed with {len(results)} steps."

    return {
        "status":  "completed",
        "summary": summary,
        "results": results,
    }


# ── Public entry point ────────────────────────────────────────────────────────

def automate_task(task_description: str, session_id: str | None = None) -> dict:
    """
    Main entry point.  Given a plain-English task description:
      1. Generate a workflow plan using GPT.
      2. Route to the correct executor based on workflow_type.
      3. Return a structured result dict.

    Raises RuntimeError on unrecoverable failures.
    """
    if not task_description or not task_description.strip():
        raise ValueError("task_description must not be empty.")

    workflow_id = session_id or f"AUTO-{uuid.uuid4().hex[:8].upper()}"
    started_at  = datetime.utcnow().isoformat()

    logger.info("[task_automation] Starting — id=%s  task='%s'", workflow_id, task_description[:80])

    # Step 1: generate plan
    try:
        plan = generate_workflow_plan(task_description)
    except ValueError as exc:
        return {
            "workflow_id":  workflow_id,
            "status":       "error",
            "error":        str(exc),
            "started_at":   started_at,
            "completed_at": datetime.utcnow().isoformat(),
        }

    workflow_type = plan.get("workflow_type", "general_task")

    # Step 2: execute
    try:
        if workflow_type == "medical_appointment":
            execution = _execute_medical_appointment(plan, task_description)
        elif workflow_type == "admission":
            execution = _execute_admission(plan)
        else:
            execution = _execute_general_task(plan, task_description)

    except Exception as exc:
        logger.error("[task_automation] Execution failed for '%s': %s", workflow_type, exc)
        return {
            "workflow_id":   workflow_id,
            "status":        "error",
            "error":         str(exc),
            "plan":          plan,
            "started_at":    started_at,
            "completed_at":  datetime.utcnow().isoformat(),
        }

    completed_at = datetime.utcnow().isoformat()
    logger.info("[task_automation] Done — id=%s  status=%s", workflow_id, execution.get("status"))

    return {
        "workflow_id":   workflow_id,
        "workflow_type": workflow_type,
        "task_name":     plan.get("task_name", ""),
        "description":   plan.get("description", ""),
        "plan":          plan,
        "result":        execution,
        "status":        execution.get("status", "completed"),
        "started_at":    started_at,
        "completed_at":  completed_at,
    }
