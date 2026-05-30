import os
import sys
import json
import uuid
import asyncio
import tempfile
from typing import Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Playwright launches Chromium via subprocess, which requires ProactorEventLoop on
# Windows. SelectorEventLoop (sometimes forced by uvicorn) lacks subprocess support
# and raises NotImplementedError() when Playwright tries to start the browser.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv()

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.intent_parser import parse_intent
from backend.workflows.appointment_workflow import run_workflow, resume_workflow
from backend.router import route_query
from backend.automation import applications_store
from backend.workflows import (
    chat_workflow,
    medical_qa_workflow,
    hr_workflow,
    document_workflow,
    university_workflow,
    property_workflow,
    student_workflow,
)
from backend.file_processing import pdf_parser, docx_parser, chunker
from backend.agents import hr_agent, rag_agent
from backend.routers.auth_router import router as auth_router
from backend.routers.admin_router import router as admin_router
from backend.routers.openai_compat import router as openai_compat_router
from backend.routers.workflow_builder import router as workflow_builder_router
from backend.database.db import (
    get_patient_by_name,
    get_all_doctors,
    get_available_slots,
    get_patient_appointments,
    get_workflow,
    create_workflow,
    update_workflow,
    create_session,
    get_sessions,
    get_session_messages,
    add_message,
    delete_session,
    update_session_title,
    save_document_record,
    get_documents,
    delete_document_record,
)

# ---------------------------------------------------------------------------
# In-memory workflow store (production would use Redis/DB)
# ---------------------------------------------------------------------------
_workflow_states: dict[str, dict] = {}
_workflow_connections: dict[str, list[WebSocket]] = {}

# Admission workflow state keyed by session_id
_session_workflows: dict[str, dict] = {}

# Generated PDFs keyed by file_id → absolute path
_generated_files: dict[str, str] = {}

# Admission automation progress queues — session_id → asyncio.Queue
_admission_queues: dict[str, asyncio.Queue] = {}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Startup] Medical AI Backend starting...")
    print(f"[Startup] OpenAI model: {os.getenv('OPENAI_WORKFLOW_MODEL', 'gpt-4o')}")
    # Create default admin account if none exists
    try:
        from backend.database.db import get_user_by_email, create_user
        from backend.auth.auth import hash_password
        if not get_user_by_email("admin@medical.ai"):
            create_user("Admin", "admin@medical.ai", hash_password("admin123"), role="admin")
            print("[Startup] Default admin created — email: admin@medical.ai  password: admin123")
        # Create new DB tables if they don't exist yet
        from backend.database.db import get_cursor
        with get_cursor(commit=True) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY, username VARCHAR(100) NOT NULL,
                    email VARCHAR(200) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) DEFAULT 'user', is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id VARCHAR(50) PRIMARY KEY, title VARCHAR(200) DEFAULT 'New Chat',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(50) REFERENCES chat_sessions(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL, content TEXT NOT NULL,
                    workflow VARCHAR(50) DEFAULT 'general', sources_json TEXT DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS uploaded_documents (
                    id VARCHAR(50) PRIMARY KEY, filename VARCHAR(255) NOT NULL,
                    file_type VARCHAR(20), chunks_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        print("[Startup] DB tables verified")
    except Exception as e:
        print(f"[Startup] DB init warning: {e}")

    # Create student tables (safe if already exist)
    try:
        from backend.database.students_db import create_student_tables
        create_student_tables()
    except Exception as e:
        print(f"[Startup] Student DB init warning: {e}")

    # Auto-scrape Riphah website if university_knowledge collection is empty
    try:
        from backend.scraper.web_scraper import is_collection_populated, trigger_background_scrape
        if not is_collection_populated():
            print("[Startup] university_knowledge empty — starting background scrape of riphah.edu.pk")
            trigger_background_scrape()
        else:
            print("[Startup] university_knowledge already populated, skipping scrape")
    except Exception as e:
        print(f"[Startup] Scrape init warning: {e}")

    yield
    print("[Shutdown] Medical AI Backend stopping...")


app = FastAPI(
    title="Medical AI Workflow System",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(openai_compat_router)
app.include_router(workflow_builder_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve portal automation screenshots at /api/screenshots/<filename>
_SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "static", "screenshots")
os.makedirs(_SCREENSHOTS_DIR, exist_ok=True)
app.mount("/api/screenshots", StaticFiles(directory=_SCREENSHOTS_DIR), name="screenshots")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PatientRequest(BaseModel):
    text: str
    patient_name: Optional[str] = ""


class SlotConfirmRequest(BaseModel):
    slot_id:     int
    doctor_name: str
    specialty:   str
    date:        str
    time:        str
    datetime_iso: Optional[str] = ""


class TaskAutomateRequest(BaseModel):
    task: str
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _broadcast(workflow_id: str, event: dict) -> None:
    sockets = _workflow_connections.get(workflow_id, [])
    dead = []
    for ws in sockets:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.remove(ws)
        


def _run_workflow_bg(workflow_id: str, intent: dict, patient_name: str) -> None:
    state = run_workflow(intent, patient_name)
    state["workflow_id"] = workflow_id
    _workflow_states[workflow_id] = state

    try:
        update_workflow(
            workflow_id,
            state.get("status", "completed"),
            json.dumps(state.get("step_results", {})),
        )
    except Exception as e:
        print(f"[DB] Failed to persist workflow: {e}")

        


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/patient/request")
async def patient_request(req: PatientRequest, background_tasks: BackgroundTasks):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Request text is empty")

    intent = await asyncio.to_thread(parse_intent, req.text)
    print(f"[API] Parsed intent: {intent}")

    import uuid
    workflow_id = f"WF-{uuid.uuid4().hex[:8].upper()}"

    patient_name = req.patient_name or ""
    patient = None
    patient_id = None
    if patient_name:
        try:
            patient = get_patient_by_name(patient_name)
            if patient:
                patient_id = patient["id"]
        except Exception:
            pass

    try:
        create_workflow(workflow_id, patient_id, intent.get("task_type", "appointment_booking"))
    except Exception as e:
        print(f"[DB] Workflow create warning: {e}")

    _workflow_states[workflow_id] = {
        "workflow_id": workflow_id,
        "status": "started",
        "intent": intent,
        "patient_name": patient_name,
        "step_results": {},
    }

    background_tasks.add_task(_run_workflow_bg, workflow_id, intent, patient_name)

    return {
        "workflow_id": workflow_id,
        "status": "started",
        "intent": intent,
    }


@app.get("/api/workflow/{workflow_id}")
async def get_workflow_status(workflow_id: str):
    state = _workflow_states.get(workflow_id)
    if not state:
        # Try DB
        try:
            db_wf = get_workflow(workflow_id)
            if db_wf:
                return {
                    "workflow_id": workflow_id,
                    "status": db_wf["status"],
                    "step_results": json.loads(db_wf.get("steps_json") or "{}"),
                }
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="Workflow not found")

    return {
        "workflow_id": workflow_id,
        "status": state.get("status", "unknown"),
        "step_results": state.get("step_results", {}),
        "available_slots": state.get("available_slots", []),
        "awaiting_input": state.get("awaiting_input", ""),
        "ui_output": state.get("ui_output", ""),
        "booking": state.get("booking", {}),
        "errors": state.get("errors", []),
        "no_specialist_notice": state.get("no_specialist_notice", ""),
    }


@app.post("/api/workflow/{workflow_id}/confirm")
async def confirm_slot(workflow_id: str, req: SlotConfirmRequest, background_tasks: BackgroundTasks):
    state = _workflow_states.get(workflow_id)
    if not state:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if state.get("status") not in ("awaiting_input", "completed", "started"):
        raise HTTPException(status_code=400, detail=f"Workflow is not awaiting input (status={state.get('status')})")

    selected_slot = {
        "slot_id":     req.slot_id,
        "doctor_name": req.doctor_name,
        "specialty":   req.specialty,
        "date":        req.date,
        "time":        req.time,
        "datetime_iso": req.datetime_iso,
    }

    def _resume():
        updated = resume_workflow(state, selected_slot)
        _workflow_states[workflow_id] = updated
        try:
            update_workflow(
                workflow_id,
                updated.get("status", "completed"),
                json.dumps(updated.get("step_results", {})),
            )
        except Exception as e:
            print(f"[DB] Resume persist warning: {e}")

    background_tasks.add_task(_resume)
    return {"workflow_id": workflow_id, "status": "confirming"}


@app.get("/api/doctors")
async def list_doctors():
    try:
        doctors = get_all_doctors()
        result = []
        for d in doctors:
            slots = get_available_slots(d["id"], limit=1)
            next_slot = None
            if slots:
                from datetime import datetime
                dt = slots[0]["datetime"]
                if isinstance(dt, str):
                    dt = datetime.fromisoformat(dt)
                next_slot = dt.strftime("%A, %B %d at %I:%M %p")
            result.append({
                "id":             d["id"],
                "name":           d["name"],
                "specialty":      d["specialty"],
                "available_days": d["available_days"],
                "next_slot":      next_slot,
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/patient/{name}/history")
async def patient_history(name: str):
    try:
        patient = get_patient_by_name(name)
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")
        history = get_patient_appointments(patient["id"])
        from datetime import datetime
        formatted = []
        for h in history:
            dt = h["datetime"]
            if isinstance(dt, str):
                dt = datetime.fromisoformat(dt)
            formatted.append({
                "id":          h["id"],
                "doctor_name": h["doctor_name"],
                "specialty":   h["specialty"],
                "date":        dt.strftime("%Y-%m-%d"),
                "time":        dt.strftime("%I:%M %p"),
                "status":      h["status"],
                "reason":      h.get("reason"),
            })
        return {"patient": dict(patient), "appointments": formatted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Student data REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/students")
async def list_students(department: Optional[str] = None, limit: int = 50):
    """List students, optionally filtered by department."""
    try:
        from backend.database.students_db import get_all_students
        rows = get_all_students(department=department, limit=limit)
        return {"students": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/students/search")
async def search_students_api(q: str):
    """Search students by name, reg_no, or department."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")
    try:
        from backend.database.students_db import search_students
        rows = search_students(q)
        return {"students": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/students/{identifier}")
async def get_student(identifier: str):
    """
    Get full student profile by name or registration number.
    Examples: /api/students/Awais  or  /api/students/RIU-2022-CS-001
    """
    try:
        from backend.database.students_db import (
            get_student_by_name, get_student_by_reg, get_student_full_profile
        )
        import re
        student = (
            get_student_by_reg(identifier)
            if re.match(r"RIU-", identifier, re.IGNORECASE)
            else get_student_by_name(identifier)
        )
        if not student:
            raise HTTPException(status_code=404, detail=f"Student '{identifier}' not found")
        profile = get_student_full_profile(student["id"])
        return profile
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/students/seed")
async def seed_student_data():
    """Seed the database with sample Riphah student data (admin use)."""
    try:
        from backend.database.seed_students import run_seed
        await asyncio.to_thread(run_seed)
        return {"status": "ok", "message": "Sample student data seeded successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    return {"message": "Medical AI System API", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health")
async def health():
    from backend.models.openai_client import health_check
    from backend.automation.schema_cache import get_cache_summary
    openai_status = await asyncio.to_thread(health_check)
    return {
        "status":         "ok",
        "openai":         openai_status,
        "workflow_count": len(_workflow_states),
        "portal_cache":   get_cache_summary(),
    }


@app.get("/api/portal/cache")
async def portal_cache_info():
    """Return summary of cached portal schemas."""
    from backend.automation.schema_cache import get_cache_summary
    return {"schemas": get_cache_summary()}


@app.delete("/api/portal/cache")
async def clear_portal_cache():
    """Force a full re-scan next time (clears the portal selector cache)."""
    from backend.automation.schema_cache import clear_schema
    from backend.automation.portal_agent import PORTAL_URL
    clear_schema(PORTAL_URL)
    return {"status": "cleared", "portal_url": PORTAL_URL}


class ScrapeRequest(BaseModel):
    urls: Optional[list[str]] = None  # None = use default Riphah seed URLs


@app.post("/api/scrape")
async def scrape_university(req: ScrapeRequest = ScrapeRequest()):
    """Trigger a background scrape of riphah.edu.pk into university_knowledge."""
    from backend.scraper.web_scraper import trigger_background_scrape, is_scraping
    if is_scraping():
        return {"status": "already_running", "message": "Scrape already in progress"}
    trigger_background_scrape(req.urls)
    urls = req.urls or ["riphah.edu.pk (default seed URLs)"]
    return {"status": "started", "message": f"Scraping {len(urls)} URL(s) in background"}


# ---------------------------------------------------------------------------
# WebSocket — real-time step streaming
# ---------------------------------------------------------------------------

@app.websocket("/api/workflow/{workflow_id}/stream")
async def workflow_stream(websocket: WebSocket, workflow_id: str):
    await websocket.accept()
    if workflow_id not in _workflow_connections:
        _workflow_connections[workflow_id] = []
    _workflow_connections[workflow_id].append(websocket)

    try:
        # Send current state immediately
        state = _workflow_states.get(workflow_id, {})
        await websocket.send_text(json.dumps({
            "type":         "state_snapshot",
            "workflow_id":  workflow_id,
            "status":       state.get("status", "unknown"),
            "step_results": state.get("step_results", {}),
        }))

        # Poll for updates and push to client
        last_step_count = len(state.get("step_results", {}))
        while True:
            await asyncio.sleep(1)
            current = _workflow_states.get(workflow_id, {})
            current_count = len(current.get("step_results", {}))

            if current_count != last_step_count or current.get("status") != state.get("status"):
                state = current
                last_step_count = current_count
                await websocket.send_text(json.dumps({
                    "type":          "update",
                    "workflow_id":   workflow_id,
                    "status":        current.get("status", "unknown"),
                    "step_results":  current.get("step_results", {}),
                    "available_slots": current.get("available_slots", []),
                    "awaiting_input": current.get("awaiting_input", ""),
                    "booking":       current.get("booking", {}),
                    "ui_output":     current.get("ui_output", ""),
                }))

                if current.get("status") in ("completed", "error", "awaiting_input"):
                    break

    except WebSocketDisconnect:
        pass
    finally:
        conns = _workflow_connections.get(workflow_id, [])
        if websocket in conns:
            conns.remove(websocket)


# ---------------------------------------------------------------------------
# Admission application persistence helpers
# ---------------------------------------------------------------------------

class _ThreadSafeProgressQueue:
    """
    A drop-in replacement for `asyncio.Queue` (only the `put_nowait` method
    is used by PortalProgressTracker) that bridges progress events from a
    worker thread / worker event loop back into the FastAPI loop's queue.

    Why this exists
    ---------------
    On Windows, Playwright needs `WindowsProactorEventLoop` to spawn
    Chromium via asyncio subprocess. uvicorn — especially with `--reload` —
    can leave the FastAPI loop on `SelectorEventLoop`, which makes
    Playwright raise `NotImplementedError`. The fix is to run Playwright
    inside a dedicated worker thread with its own Proactor loop. But the
    PortalProgressTracker still needs to push step updates to the
    WebSocket handler (which lives on the FastAPI loop). This wrapper
    handles that handoff safely.
    """

    def __init__(self, main_loop: asyncio.AbstractEventLoop, target_queue: asyncio.Queue):
        self._loop  = main_loop
        self._queue = target_queue

    def put_nowait(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except Exception as e:
            print(f"[AdmissionBridge] dropped progress event: {e}")

    async def put(self, item) -> None:
        # Provided for completeness — same semantics as put_nowait here.
        self.put_nowait(item)


def _launch_admission_automation_thread(
    session_id: str,
    wf_state: dict,
    fastapi_queue: asyncio.Queue,
    main_loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Spawn a daemon thread that runs the Playwright automation inside its own
    `WindowsProactorEventLoop`. Progress events bubble back to the FastAPI
    loop via `_ThreadSafeProgressQueue`, the existing WebSocket consumer is
    completely unchanged.
    """
    import threading

    def _worker():
        # Each thread needs its own policy + loop on Windows
        if sys.platform == "win32":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        bridge_q = _ThreadSafeProgressQueue(main_loop, fastapi_queue)

        async def _run():
            from backend.automation.portal_agent import run_automation_async, PORTAL_URL
            try:
                result = await run_automation_async(
                    data=wf_state["collected_data"],
                    uploaded_docs=wf_state.get("docs_uploaded", {}),
                    progress_queue=bridge_q,
                    portal_url=PORTAL_URL,
                )
                # Convert screenshot file paths → /api/screenshots/<filename> URLs
                screenshot_urls = []
                for ss_item in result.get("screenshots", []):
                    ss_path  = ss_item.get("path", "") if isinstance(ss_item, dict) else ss_item
                    ss_label = ss_item.get("label", "Screenshot") if isinstance(ss_item, dict) else "Screenshot"
                    if ss_path and os.path.isfile(ss_path):
                        filename = os.path.basename(ss_path)
                        screenshot_urls.append({"url": f"/api/screenshots/{filename}", "label": ss_label})
                result["screenshot_urls"] = screenshot_urls
                wf_state["phase"] = "complete"
                wf_state["automation_result"] = result
                _persist_admission(session_id, wf_state, result)
                _append_credentials_to_chat(session_id, wf_state, result)
                bridge_q.put_nowait({"done": True, "result": result})
            except Exception as e:
                import traceback
                traceback.print_exc()
                wf_state["phase"] = "error"
                err_result = {
                    "success": False,
                    "message": f"Portal automation failed — **{type(e).__name__}**: {e or repr(e)}",
                    "screenshot_urls": [],
                }
                wf_state["automation_result"] = err_result
                _persist_admission(session_id, wf_state, err_result)
                bridge_q.put_nowait({"done": True, "result": err_result})
            finally:
                _session_workflows[session_id] = wf_state

        try:
            loop.run_until_complete(_run())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True, name=f"admission-{session_id}").start()


def _persist_admission(session_id: str, state: dict, result: dict) -> None:
    """Save the admission attempt to the local JSON store so the user can look
    it up later — even after a restart or new chat session."""
    try:
        data = state.get("collected_data", {}) or {}
        status = "submitted"
        if result.get("needs_verification"):
            status = "needs_verification"
        elif not result.get("success"):
            status = "failed"

        record = {
            "application_id":     state.get("application_id"),
            "session_id":         session_id,
            "full_name":          data.get("full_name"),
            "email":              data.get("email"),
            "cnic":               data.get("cnic"),
            "phone":              data.get("phone"),
            "program":            data.get("program"),
            "campus":             data.get("campus"),
            "portal_email":       result.get("portal_email") or data.get("email"),
            "portal_password":    result.get("portal_password") or data.get("portal_password"),
            "reference":          result.get("reference"),
            "dashboard_url":      result.get("dashboard_url") or "https://admissions.riphah.edu.pk/",
            "status":             status,
            "needs_verification": bool(result.get("needs_verification")),
            "verification_type":  result.get("verification_type"),
            "message":            result.get("message"),
        }
        applications_store.save_application(record)
    except Exception as e:
        print(f"[Admission] Persist failed: {e}")


def _append_credentials_to_chat(session_id: str, state: dict, result: dict) -> None:
    """
    Also write the credentials/reference into the chat history as an assistant
    message so they survive a page reload, even if the AdmissionProgress side
    widget is dismissed.
    """
    try:
        portal_email    = result.get("portal_email") or state.get("collected_data", {}).get("email")
        portal_password = result.get("portal_password") or state.get("collected_data", {}).get("portal_password")
        dashboard_url   = result.get("dashboard_url") or "https://admissions.riphah.edu.pk/"
        reference       = result.get("reference")
        app_id          = state.get("application_id")

        lines = [
            "**📌 Saved for later — your Riphah portal access:**",
            f"• **Portal URL:** {dashboard_url}",
            f"• **Email:** {portal_email}",
            f"• **Password:** `{portal_password}`",
            f"• **Local Application ID:** {app_id}",
        ]
        if reference:
            lines.append(f"• **Portal Reference:** `{reference}`")
        lines.append("")
        lines.append(
            "You can ask me later by saying **'show my application'** or "
            "**'what was my Riphah password'** and I'll bring this back."
        )
        add_message(session_id, "assistant", "\n".join(lines), "admission", [])
    except Exception as e:
        print(f"[Admission] Could not append credentials to chat history: {e}")


_ADMISSION_LOOKUP_KEYWORDS = [
    "show my application",
    "show my applications",
    "my application status",
    "my admission status",
    "my riphah application",
    "my riphah account",
    "my portal credentials",
    "my portal password",
    "what was my password",
    "what is my password",
    "what was my riphah password",
    "what's my riphah password",
    "where is my application",
    "retrieve my application",
    "admission credentials",
]


def _looks_like_admission_lookup(text: str) -> bool:
    lower = text.lower().strip()
    return any(kw in lower for kw in _ADMISSION_LOOKUP_KEYWORDS)


def _format_admission_record(rec: dict) -> str:
    if not rec:
        return ""
    status_emoji = {
        "submitted":           "✅",
        "needs_verification":  "⚠",
        "failed":              "❌",
    }.get(rec.get("status", ""), "•")

    parts = [
        f"{status_emoji} **Your Riphah admission application**",
        "",
        f"• **Local Application ID:** {rec.get('application_id', '—')}",
        f"• **Status:** {rec.get('status', 'unknown')}",
        f"• **Program / Campus:** {rec.get('program', '—')} — {rec.get('campus', '—')}",
        f"• **Applicant:** {rec.get('full_name', '—')}",
        "",
        "**Portal access:**",
        f"• **URL:** {rec.get('dashboard_url', 'https://admissions.riphah.edu.pk/')}",
        f"• **Email:** {rec.get('portal_email', '—')}",
        f"• **Password:** `{rec.get('portal_password', '—')}`",
    ]
    if rec.get("reference"):
        parts.append(f"• **Portal Reference:** `{rec.get('reference')}`")
    if rec.get("needs_verification"):
        vt = rec.get("verification_type") or "email"
        parts.append("")
        parts.append(
            f"⚠ The portal asked for **{vt} verification**. Complete that step "
            "in the portal before logging in."
        )
    parts.append("")
    parts.append(f"_Recorded at: {rec.get('updated_at', rec.get('created_at', '—'))}_")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Admission automation — real-time progress WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/api/admission/{session_id}/stream")
async def admission_stream(websocket: WebSocket, session_id: str):
    """
    Stream admission automation progress steps to the frontend.
    The client connects immediately after receiving admission_session_id in a chat response.
    """
    await websocket.accept()

    # Wait for queue to appear (automation may start a moment after WS connects)
    for _ in range(30):
        if session_id in _admission_queues:
            break
        await asyncio.sleep(0.3)

    queue = _admission_queues.get(session_id)
    if not queue:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Automation queue not found. Automation may have already completed.",
        }))
        return

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=120)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
                continue

            if item.get("done"):
                result = item.get("result", {})
                await websocket.send_text(json.dumps({
                    "type":              "complete",
                    "success":           result.get("success", False),
                    "message":           result.get("message", ""),
                    "reference":         result.get("reference"),
                    "screenshot_urls":   result.get("screenshot_urls", []),
                    "portal_password":   result.get("portal_password"),
                    "portal_email":      result.get("portal_email"),
                    "dashboard_url":     result.get("dashboard_url"),
                    "needs_verification": result.get("needs_verification", False),
                    "verification_type": result.get("verification_type"),
                }))
                break
            else:
                step_payload: dict = {
                    "type":      "step",
                    "message":   item.get("message", ""),
                    "success":   item.get("success", True),
                    "step_type": item.get("type", "step"),
                }
                # If tracker sent a screenshot file, include the URL so the
                # frontend can render it inline as the automation runs
                ss_file = item.get("screenshot_file")
                if ss_file:
                    step_payload["screenshot_url"] = f"/api/screenshots/{ss_file}"
                await websocket.send_text(json.dumps(step_payload))

    except WebSocketDisconnect:
        pass
    finally:
        _admission_queues.pop(session_id, None)


# ---------------------------------------------------------------------------
# Admission application retrieval endpoints
# ---------------------------------------------------------------------------

@app.get("/api/admissions")
async def list_admissions():
    """List all saved admission applications (newest first)."""
    return applications_store.list_all(limit=100)


@app.get("/api/admissions/{key}")
async def get_admission(key: str):
    """
    Look up a saved admission application by either:
      • application_id  (e.g. RIU-A1B2C3D4)
      • session_id      (chat session id)
    """
    rec = (
        applications_store.get_by_application_id(key)
        or applications_store.get_by_session(key)
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No application found")
    return rec


# ---------------------------------------------------------------------------
# Unified Chat API
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    text: str
    session_id: Optional[str] = None
    document_id: Optional[str] = None
    category: Optional[str] = None  # 'university' | 'medical' | 'property'


def _run_workflow(workflow: str, text: str, hist: list, doc_id: str | None) -> dict:
    if workflow == "student_data":
        return student_workflow.run(text, hist)
    if workflow == "medical_qa":
        return medical_qa_workflow.run(text, hist)
    if workflow == "hr_tasks":
        return hr_workflow.run(text, hist)
    if workflow == "university":
        return university_workflow.run(text, hist)
    if workflow == "property":
        return property_workflow.run(text, hist)
    if workflow == "document_chat":
        return document_workflow.run(text, doc_id or "", hist)
    return chat_workflow.run(text, hist)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Message is empty")

    session_id = req.session_id or uuid.uuid4().hex[:12]
    history: list[dict] = []

    # Ensure session exists and load history
    try:
        create_session(session_id, req.text[:60])
        rows = get_session_messages(session_id)
        history = [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print(f"[Chat] DB session error: {e}")

    # -----------------------------------------------------------------------
    # Admission lookup intent ("show my application", "what was my password")
    # — handled BEFORE routing so the user can ask in any context.
    # Only when no admission workflow is currently mid-flight.
    # -----------------------------------------------------------------------
    if session_id not in _session_workflows and _looks_like_admission_lookup(req.text):
        rec = (
            applications_store.get_by_session(session_id)
            or applications_store.find_application(session_id=session_id)
        )
        if rec is None:
            # As a fallback, return the most recent application globally
            all_apps = applications_store.list_all(limit=1)
            rec = all_apps[0] if all_apps else None

        if rec:
            response_text = _format_admission_record(rec)
        else:
            response_text = (
                "I don't have any saved admission applications yet. "
                "Say **'apply for admission'** to start one — I'll save your "
                "credentials and reference number so you can look them up later."
            )
        try:
            add_message(session_id, "user", req.text, "admission")
            add_message(session_id, "assistant", response_text, "admission", [])
        except Exception as e:
            print(f"[Chat] DB message save error: {e}")
        return {
            "session_id": session_id,
            "response":   response_text,
            "workflow":   "admission",
            "sources":    [],
            "admission_record": rec,
        }

    # -----------------------------------------------------------------------
    # Admission workflow interception (active workflow in flight)
    # -----------------------------------------------------------------------
    if session_id in _session_workflows:
        wf_state = _session_workflows[session_id]
        phase    = wf_state.get("phase", "")
        data     = wf_state.get("collected_data", {})

        # ── While automation is running ───────────────────────────────────────
        if phase == "automating":
            name = data.get("full_name", "applicant")
            prog = data.get("program", "")
            campus = data.get("campus", "")
            response_text = (
                f"⏳ **Automation is running** — submitting {name}'s application for **{prog}** ({campus}).\n\n"
                "Live progress is shown in the panel below. Please wait, this takes 1–3 minutes.\n\n"
                "*Tip: do not close this tab.*"
            )
            try:
                add_message(session_id, "user",      req.text,       "admission")
                add_message(session_id, "assistant",  response_text,  "admission", [])
            except Exception:
                pass
            return {
                "session_id":           session_id,
                "response":             response_text,
                "workflow":             "admission",
                "sources":              [],
                "admission_session_id": session_id,
                "collected_data":       data,
            }

        # ── Advance the workflow ──────────────────────────────────────────────
        from backend.workflows import admission_workflow
        try:
            wf_state, response_text, is_terminal = await asyncio.to_thread(
                admission_workflow.advance, wf_state, req.text
            )
        except Exception as exc:
            print(f"[Chat] Admission advance error: {exc}")
            # Don't lose the state — surface the error and keep the session alive
            response_text = (
                "⚠️ An unexpected error occurred while processing your input.\n\n"
                f"Error: `{exc}`\n\n"
                "Your progress is saved. Please try your last answer again, or type **'cancel'** to start over."
            )
            is_terminal = False

        _session_workflows[session_id] = wf_state
        new_phase = wf_state.get("phase", "")
        extra: dict = {}

        # ── Automation triggered ──────────────────────────────────────────────
        if new_phase == "automating":
            extra["admission_session_id"] = session_id
            q: asyncio.Queue = asyncio.Queue()
            _admission_queues[session_id] = q
            main_loop = asyncio.get_running_loop()
            _launch_admission_automation_thread(session_id, wf_state, q, main_loop)

        # ── Automation just finished (phase flipped by background thread) ─────
        if new_phase == "complete":
            result = wf_state.get("automation_result", {})
            response_text = result.get("message", response_text)

        # ── Automation errored (phase set by background thread) ───────────────
        if new_phase == "error" and not is_terminal:
            # Store the error message in state for the error-phase handler
            auto_result = wf_state.get("automation_result", {})
            if auto_result and not wf_state.get("automation_error"):
                wf_state["automation_error"] = auto_result.get("message", "Automation failed.")
                _session_workflows[session_id] = wf_state
            # Do NOT pop state yet — let user retry or cancel from the error phase

        # ── Terminal: only remove state after true completion or cancel ───────
        if is_terminal and new_phase in ("cancelled", "complete"):
            _session_workflows.pop(session_id, None)
            _admission_queues.pop(session_id, None)
        # Note: "error" phase is NOT terminal here — user can retry via the error handler

        # ── Attach collected data summary to every response ───────────────────
        collected = wf_state.get("collected_data", {})
        fields_done = [k for k, v in collected.items() if v and k != "portal_password_source"]

        try:
            add_message(session_id, "user",      req.text,      "admission")
            add_message(session_id, "assistant",  response_text, "admission", [])
        except Exception as e:
            print(f"[Chat] DB message save error: {e}")

        return {
            "session_id":     session_id,
            "response":       response_text,
            "workflow":       "admission",
            "sources":        [],
            "phase":          new_phase,
            "fields_done":    len(fields_done),
            "collected_data": {k: v for k, v in collected.items() if k != "portal_password"},
            **extra,
        }

    # -----------------------------------------------------------------------
    # Normal routing
    # -----------------------------------------------------------------------
    workflow = route_query(
        req.text,
        has_active_document=bool(req.document_id),
        category=req.category,
    )

    response_text = ""
    sources: list = []
    extra_appt: dict = {}

    if workflow == "medical_appointment":
        # Route appointments via the existing appointment workflow machinery.
        try:
            intent = parse_intent(req.text)
            patient_name = req.text  # best-effort; real apps would resolve from auth
            workflow_id  = uuid.uuid4().hex[:12]
            background_tasks_ok = True
            # Kick off the appointment workflow in background
            import threading
            threading.Thread(
                target=_run_workflow_bg,
                args=(workflow_id, intent, patient_name),
                daemon=True,
            ).start()
            extra_appt["appointment_workflow_id"] = workflow_id
            response_text = (
                "I'm setting up your appointment booking. "
                "Live updates will appear shortly."
            )
        except Exception as e:
            print(f"[Chat] appointment dispatch error: {e}")
            response_text = "Sorry — I couldn't start the appointment workflow."

        try:
            add_message(session_id, "user", req.text, workflow)
            add_message(session_id, "assistant", response_text, workflow, [])
        except Exception as e:
            print(f"[Chat] DB message save error: {e}")

        return {
            "session_id": session_id,
            "response":   response_text,
            "workflow":   "medical_appointment",
            "sources":    [],
            **extra_appt,
        }

    else:
        result = await asyncio.to_thread(_run_workflow, workflow, req.text, history, req.document_id)
        response_text = result.get("response", "")
        sources       = result.get("sources", [])

        # Check if the university workflow wants to trigger an admission form
        if result.get("trigger_workflow") == "admission":
            from backend.workflows import admission_workflow
            wf_state = admission_workflow.start(session_id)
            _session_workflows[session_id] = wf_state
            intro = admission_workflow.get_intro_message()
            response_text = result["response"] + "\n\n" + intro

    # Save messages to DB
    try:
        add_message(session_id, "user", req.text, workflow)
        add_message(session_id, "assistant", response_text, workflow, sources)
    except Exception as e:
        print(f"[Chat] DB message save error: {e}")

    return {
        "session_id": session_id,
        "response":   response_text,
        "workflow":   workflow,
        "sources":    sources,
    }


# ---------------------------------------------------------------------------
# File download (PDFs, screenshots, etc.)
# ---------------------------------------------------------------------------

@app.get("/api/download/{file_id}")
async def download_file(file_id: str):
    """Serve a generated file (PDF report, screenshot, etc.) by its file_id."""
    path = _generated_files.get(file_id)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    filename = os.path.basename(path)
    # Choose a sensible media_type based on extension
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = {
        "pdf":  "application/pdf",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def list_sessions():
    try:
        rows = get_sessions()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Sessions] list error: {e}")
        return []


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    try:
        rows = get_session_messages(session_id)
        messages = []
        for r in rows:
            msg = dict(r)
            try:
                msg["sources"] = json.loads(msg.pop("sources_json", "[]"))
            except Exception:
                msg["sources"] = []
            messages.append(msg)
        return {"id": session_id, "messages": messages}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/sessions/{session_id}")
async def remove_session(session_id: str):
    try:
        delete_session(session_id)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# File upload endpoint (PDF / DOCX -> RAG)
# ---------------------------------------------------------------------------

def _extract_text(file_path: str, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return pdf_parser.extract_text(file_path)
    if ext in ("docx", "doc"):
        return docx_parser.extract_text(file_path)
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a PDF or DOCX, extract text, chunk it, ingest into the RAG store."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Save to a temp file
    suffix = "." + file.filename.rsplit(".", 1)[-1] if "." in file.filename else ""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        text = _extract_text(tmp.name, file.filename)
        if not text.strip():
            raise HTTPException(status_code=400, detail="Could not extract any text from the file")

        chunks = chunker.chunk_text(text)
        doc_id = uuid.uuid4().hex[:12]

        # Ingest into RAG
        try:
            from backend.rag.embeddings import ingest_chunks
            ingest_chunks(doc_id, file.filename, chunks)
        except Exception as e:
            print(f"[Upload] RAG ingest error: {e}")

        # Record document
        try:
            ext = file.filename.rsplit(".", 1)[-1].lower()
            save_document_record(doc_id, file.filename, ext, len(chunks))
        except Exception as e:
            print(f"[Upload] DB record error: {e}")

        return {
            "document_id": doc_id,
            "filename":    file.filename,
            "chunks":      len(chunks),
        }
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@app.get("/api/documents")
async def list_documents():
    try:
        return [dict(r) for r in get_documents()]
    except Exception as e:
        print(f"[Documents] list error: {e}")
        return []


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: str):
    try:
        delete_document_record(doc_id)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Task Automation Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/task/automate")
async def automate_task_endpoint(req: TaskAutomateRequest):
    """
    Describe any task in plain English and the system will:
      1. Generate a structured workflow plan using GPT-4o.
      2. Execute the workflow using the appropriate agents.
      3. Return the plan + result.

    Examples:
      {"task": "Book a cardiology appointment for John Smith with chest pain urgently"}
      {"task": "Apply for MBBS admission at Riphah for Ahsan Khan"}
      {"task": "Summarise the leave policy and book 3 days leave for next week"}
    """
    from backend.workflows.task_automation_workflow import automate_task

    task = (req.task or "").strip()
    if not task:
        raise HTTPException(status_code=422, detail="'task' field must not be empty.")

    if len(task) > 2000:
        raise HTTPException(status_code=422, detail="Task description too long (max 2000 characters).")

    try:
        result = await asyncio.to_thread(
            automate_task, task, req.session_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    if result.get("status") == "error":
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Workflow execution failed."),
        )

    return result


@app.post("/api/task/plan")
async def generate_task_plan(req: TaskAutomateRequest):
    """
    Generate a workflow plan for a task WITHOUT executing it.
    Useful to preview the steps before committing to execution.
    """
    from backend.workflows.task_automation_workflow import generate_workflow_plan

    task = (req.task or "").strip()
    if not task:
        raise HTTPException(status_code=422, detail="'task' field must not be empty.")

    try:
        plan = await asyncio.to_thread(generate_workflow_plan, task)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {exc}")

    return {"status": "ok", "plan": plan}
