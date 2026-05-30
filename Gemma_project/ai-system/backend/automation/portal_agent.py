"""
Playwright-based Riphah Admission Portal Automation Agent.

Runs in a VISIBLE browser (headless=False) so the full workflow is observable.
Screenshots are captured at every major step and served via /api/screenshots/.
"""

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

PORTAL_URL = os.getenv("RIPHAH_PORTAL_URL", "https://admissions.riphah.edu.pk/riphah_demo/public/")

# Exact sub-URLs discovered by live DOM inspection
_LOGIN_URL = PORTAL_URL.rstrip("/") + "/"
_REG_URL   = PORTAL_URL.rstrip("/") + "/account-registration"

SHORT_TIMEOUT = 2_000   # ms — per-locator timeout
NAV_TIMEOUT   = 30_000  # ms — page.goto() timeout

# Directory where screenshots are saved and served from
SCREENSHOTS_DIR = Path(__file__).parent.parent / "static" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Fallback field list when portal is unreachable
DEFAULT_FIELDS = [
    # ── Identity ─────────────────────────────────────────────────────────────
    {"key": "full_name",       "label": "First + Last Name (as on CNIC)",       "type": "text",   "required": True},
    {"key": "middle_name",     "label": "Middle Name (optional)",                "type": "text",   "required": False},
    {"key": "father_name",     "label": "Father's Full Name",                    "type": "text",   "required": True},
    {"key": "cnic",            "label": "CNIC / B-Form Number",                  "type": "text",   "required": True},
    {"key": "dob",             "label": "Date of Birth (DD/MM/YYYY)",            "type": "date",   "required": True},
    {"key": "gender",          "label": "Gender",                                "type": "select", "required": True,  "options": ["Male", "Female"]},
    {"key": "nationality",     "label": "Nationality",                           "type": "text",   "required": False, "default": "Pakistan"},
    # ── Contact ───────────────────────────────────────────────────────────────
    {"key": "email",           "label": "Email Address",                         "type": "email",  "required": True},
    {"key": "phone",           "label": "Mobile Number",                         "type": "phone",  "required": True},
    {"key": "alternate_phone", "label": "Alternate / WhatsApp Number",           "type": "phone",  "required": True},
    # ── Address ───────────────────────────────────────────────────────────────
    {"key": "address",         "label": "Current Residential Address",           "type": "text",   "required": True},
    {"key": "city",            "label": "City",                                  "type": "text",   "required": True},
    # ── Academic ──────────────────────────────────────────────────────────────
    {"key": "last_institute",  "label": "College / Last Institute Attended",     "type": "text",   "required": True},
    {"key": "matric_marks",    "label": "Matric / O-Level Marks or %",           "type": "text",   "required": True},
    {"key": "inter_marks",     "label": "Intermediate / A-Level Marks or %",     "type": "text",   "required": True},
    {"key": "entry_test",      "label": "Entry Test Score (MDCAT / ECAT / NAT)", "type": "text",   "required": True},
    # ── Program ───────────────────────────────────────────────────────────────
    {"key": "campus",          "label": "Preferred Campus",                      "type": "select", "required": True,  "options": ["Islamabad/Rawalpindi", "Lahore", "Malakand"]},
    {"key": "level",           "label": "Program Level",                         "type": "select", "required": True,  "options": ["Undergraduate", "Postgraduate", "MS/MPhil", "PhD", "Diploma/Certificate"]},
    {"key": "program",         "label": "Program Preference 1 (main program)",   "type": "text",   "required": True},
    {"key": "program2",        "label": "Program Preference 2 (optional)",       "type": "text",   "required": False},
    {"key": "program3",        "label": "Program Preference 3 (optional)",       "type": "text",   "required": False},
    {"key": "program4",        "label": "Program Preference 4 (optional)",       "type": "text",   "required": False},
    # ── Source ────────────────────────────────────────────────────────────────
    {"key": "heard_from",      "label": "How did you hear about us?",            "type": "select", "required": True,
     "options": ["Facebook", "Instagram", "Newspaper", "Friend or Family", "YouTube", "Google", "Poster/Banners"]},
]

# Human-readable labels → common portal identifiers (fallback labels)
FIELD_LABELS: dict[str, list[str]] = {
    "full_name":    ["full name", "applicant name", "name"],
    "father_name":  ["father", "father's name", "guardian"],
    "cnic":         ["cnic", "national id", "nic"],
    "dob":          ["date of birth", "dob", "birth date"],
    "gender":       ["gender", "sex"],
    "email":        ["email", "email address"],
    "phone":        ["phone", "mobile", "contact"],
    "program":      ["program", "programme", "degree", "course"],
    "campus":       ["campus", "location", "preferred campus"],
    "matric_marks": ["matric", "ssc", "secondary"],
    "inter_marks":  ["intermediate", "hssc", "inter"],
    "entry_test":   ["entry test", "mdcat", "ecat", "nat"],
    "address":      ["address", "home address", "permanent address"],
}


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

class PortalProgressTracker:
    def __init__(self):
        self.steps: list[dict] = []
        self._queues: list[asyncio.Queue] = []

    def add_queue(self, q: asyncio.Queue):
        self._queues.append(q)

    def add(self, message: str, success: bool = True, step_type: str = "step", extra: dict | None = None):
        step = {"message": message, "success": success, "type": step_type, "timestamp": time.time()}
        if extra:
            step.update(extra)
        self.steps.append(step)
        for q in self._queues:
            try:
                q.put_nowait(step)
            except Exception:
                pass
        icon = "[OK]" if success else "[FAIL]"
        safe = f"[PortalAgent] {icon} {message}".encode("ascii", errors="replace").decode("ascii")
        print(safe, flush=True)


# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------

class AdmissionPortalAgent:

    def __init__(self, tracker: PortalProgressTracker):
        self.t  = tracker
        self._pw      = None
        self._browser = None
        self.page     = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _start(self):
        from playwright.async_api import async_playwright
        self._pw      = await async_playwright().start()
        # headless=False → visible browser so the user can watch each step
        self._browser = await self._pw.chromium.launch(
            headless=False,
            slow_mo=400,  # slight delay so navigation is human-readable
            args=["--start-maximized"],
        )
        ctx = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            no_viewport=False,
        )
        self.page = await ctx.new_page()
        # Global timeout — overridden per-operation
        self.page.set_default_timeout(SHORT_TIMEOUT)

    async def _stop(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def _go(self, url: str):
        await self.page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(800)

    # ── DOM helpers ──────────────────────────────────────────────────────────

    async def _dom_snapshot(self) -> dict:
        """Return all visible interactive elements as a structured dict."""
        try:
            return await self.page.evaluate("""() => {
                const lbl = el => {
                    if (el.id) {
                        const l = document.querySelector('label[for="'+el.id+'"]');
                        if (l) return l.innerText.trim().slice(0,60);
                    }
                    return el.getAttribute('aria-label')||el.placeholder||el.name||'';
                };
                const vis = e => e.offsetWidth > 0 && e.offsetHeight > 0;
                const sel = e => {
                    if (e.name) return e.tagName.toLowerCase()+'[name="'+e.name+'"]';
                    if (e.id)   return e.tagName.toLowerCase()+'#'+e.id;
                    return '';
                };
                return {
                    url:   location.href,
                    title: document.title,
                    inputs: Array.from(document.querySelectorAll('input')).filter(vis).map(e=>({
                        sel:sel(e), type:e.type, name:e.name, id:e.id,
                        placeholder:e.placeholder, label:lbl(e), required:e.required
                    })),
                    selects: Array.from(document.querySelectorAll('select')).filter(vis).map(e=>({
                        sel:sel(e), name:e.name, id:e.id, label:lbl(e),
                        options:Array.from(e.options).slice(0,25).map(o=>({value:o.value,text:o.text.trim()}))
                    })),
                    textareas: Array.from(document.querySelectorAll('textarea')).filter(vis).map(e=>({
                        sel:sel(e), name:e.name, id:e.id, label:lbl(e), placeholder:e.placeholder
                    })),
                    buttons: Array.from(document.querySelectorAll('button,[type=submit],[role=button]')).filter(vis).map(e=>({
                        text:e.innerText.trim().slice(0,80), id:e.id, type:e.getAttribute('type')
                    })),
                    links: Array.from(document.querySelectorAll('a[href]')).filter(e=>vis(e)&&e.innerText.trim()).map(e=>({
                        text:e.innerText.trim().slice(0,60), href:e.href
                    })).slice(0,30),
                    file_inputs: Array.from(document.querySelectorAll('input[type=file]')).filter(vis).map(e=>({
                        sel:sel(e), name:e.name, id:e.id, label:lbl(e)
                    }))
                };
            }""")
        except Exception:
            return {"url":"","title":"","inputs":[],"selects":[],"textareas":[],"buttons":[],"links":[],"file_inputs":[]}

    # ── Fast interaction ─────────────────────────────────────────────────────

    async def _fast_fill(self, selector: str, value: str) -> bool:
        """Fill using an exact CSS selector with SHORT_TIMEOUT."""
        if not selector:
            return False
        try:
            await self.page.locator(selector).fill(value, timeout=SHORT_TIMEOUT)
            return True
        except Exception:
            return False

    async def _click(self, candidates: list[str]) -> bool:
        """Click first matching button/link (SHORT_TIMEOUT per attempt)."""
        for text in candidates:
            for fn in [
                lambda t=text: self.page.get_by_role("button", name=t, exact=False).first.click(timeout=SHORT_TIMEOUT),
                lambda t=text: self.page.get_by_role("link",   name=t, exact=False).first.click(timeout=SHORT_TIMEOUT),
                lambda t=text: self.page.get_by_text(t, exact=False).first.click(timeout=SHORT_TIMEOUT),
            ]:
                try:
                    await fn()
                    await self.page.wait_for_timeout(1500)
                    return True
                except Exception:
                    continue
        return False

    # ── Smart field mapping ──────────────────────────────────────────────────

    async def _llm_map_fields(self, dom: dict, data_keys: list[str]) -> dict:
        """
        Single LLM call: given DOM elements, return {data_key -> css_selector}.
        Falls back to keyword matching if LLM fails.
        """
        fillable = []
        for inp in dom.get("inputs", []):
            if inp.get("type") not in ("hidden", "submit", "button", "file", "checkbox", "radio") and inp.get("sel"):
                fillable.append({"sel": inp["sel"], "label": inp.get("label",""), "placeholder": inp.get("placeholder",""), "name": inp.get("name","")})
        for ta in dom.get("textareas", []):
            if ta.get("sel"):
                fillable.append({"sel": ta["sel"], "label": ta.get("label",""), "placeholder": ta.get("placeholder","")})

        if not fillable:
            return {}

        from backend.models.openai_client import generate

        prompt = (
            f"Map these data fields to the correct HTML form elements.\n"
            f"Data fields: {json.dumps(data_keys)}\n"
            f"Form elements: {json.dumps(fillable[:20], separators=(',',':'))}\n\n"
            "Return JSON only — only include high-confidence mappings:\n"
            "{\"full_name\": \"input[name=\\\"applicant_name\\\"]\", \"email\": \"input[name=\\\"email\\\"]\", ...}"
        )
        try:
            resp = generate("phi4-mini", prompt, "Form field mapper. Return JSON mapping only. No explanation.")
            m = re.search(r'\{[\s\S]*?\}', resp)
            if m:
                result = json.loads(m.group())
                # Validate: only keep entries where value looks like a CSS selector
                return {k: v for k, v in result.items() if isinstance(v, str) and ("[" in v or "#" in v or v.startswith("input") or v.startswith("textarea"))}
        except Exception:
            pass

        # Keyword fallback
        mapping = {}
        for key in data_keys:
            labels = FIELD_LABELS.get(key, [key.replace("_", " ")])
            for fi in fillable:
                fi_text = (fi.get("label","") + " " + fi.get("placeholder","") + " " + fi.get("name","")).lower()
                if any(lbl in fi_text for lbl in labels):
                    mapping[key] = fi["sel"]
                    break
        return mapping

    # ── Portal scan ──────────────────────────────────────────────────────────

    async def scan_portal(self, url: str) -> dict:
        """Open the portal, discover required fields via DOM + LLM. Cache result."""
        try:
            await self._start()
            await self._go(url)
            dom = await self._dom_snapshot()

            from backend.models.openai_client import generate

            prompt = (
                f"University portal: {dom.get('title','')} | {dom.get('url','')}\n"
                f"Inputs: {json.dumps(dom.get('inputs',[])[:10], separators=(',',':'))}\n"
                f"Selects: {json.dumps(dom.get('selects',[])[:5], separators=(',',':'))}\n"
                f"File inputs: {json.dumps(dom.get('file_inputs',[]), separators=(',',':'))}\n\n"
                "List all form fields the applicant must fill. JSON only:\n"
                '{"fields":[{"key":"snake_key","label":"Human Label","type":"text|email|phone|date|select|file","required":true}],'
                '"required_documents":["cnic","photo","matric","inter"],"has_signup":true}'
            )
            try:
                resp = generate("phi4-mini", prompt, "Form analyzer. JSON only.")
                m = re.search(r'\{[\s\S]*?\}', resp)
                if m:
                    parsed = json.loads(m.group())
                    if isinstance(parsed.get("fields"), list) and len(parsed["fields"]) >= 3:
                        await self._stop()
                        return parsed
            except Exception:
                pass

            await self._stop()
        except Exception as e:
            print(f"[PortalAgent] Scan error: {e}")
            try:
                await self._stop()
            except Exception:
                pass

        return {"fields": DEFAULT_FIELDS, "required_documents": ["cnic","photo","matric","inter"], "has_signup": True, "scan_failed": True}

    # ── Screenshot helpers ───────────────────────────────────────────────────

    async def _screenshot(self, label: str) -> str:
        """Capture current page. Returns absolute file path (empty on failure)."""
        filename = f"riphah_{label}_{int(time.time())}.png"
        path = str(SCREENSHOTS_DIR / filename)
        try:
            await self.page.screenshot(path=path, full_page=False, timeout=8000)
            return path
        except Exception:
            return ""

    async def _screenshot_step(self, label: str, caption: str) -> str:
        """Take screenshot AND emit a 'screenshot' tracker step with inline URL."""
        path = await self._screenshot(label)
        if path:
            filename = os.path.basename(path)
            self.t.add(
                caption,
                success=True,
                step_type="screenshot",
                extra={"screenshot_file": filename},
            )
        return path

    # ── Auth helpers (exact selectors discovered by live DOM inspection) ─────

    async def _auth_login(self, email: str, password: str, login_url: str = _LOGIN_URL) -> bool:
        """Fill the Riphah login form. Returns True on successful redirect to dashboard."""
        t = self.t
        t.add(f"Opening login page: {login_url}")
        await self._go(login_url)
        await self._screenshot_step("login_page", "Screenshot: Login page loaded")

        t.add(f"Filling email: {email}")
        e_ok = await self._fast_fill("input[name='email']", email)
        if not e_ok:
            await self._screenshot_step("login_no_email", "Screenshot: email field not found")
            t.add("Email field not found — portal may be down", success=False)
            return False

        t.add("Filling password")
        p_ok = await self._fast_fill("input[name='password']", password)
        if not p_ok:
            t.add("Password field not found", success=False)
            return False

        await self._screenshot_step("login_filled", "Screenshot: Login form filled")
        t.add("Clicking Login button")
        await self._click(["Login", "Log In", "Sign In"])
        await self.page.wait_for_timeout(5000)  # give portal time to process login

        url  = self.page.url
        body = (await self.page.inner_text("body")).lower()
        t.add(f"After login: {url}")

        if url.rstrip("/") != login_url.rstrip("/"):
            if "incorrect" in body or "invalid credentials" in body:
                await self._screenshot_step("login_rejected", "Screenshot: Login rejected")
                t.add("Login rejected — credentials not accepted", success=False)
                return False
            await self._screenshot_step("login_success", "Screenshot: Login successful — dashboard")
            t.add("Login confirmed — redirected to dashboard")
            return True

        if "incorrect" in body or "invalid" in body or "wrong password" in body or "these credentials" in body:
            await self._screenshot_step("login_error", "Screenshot: Login error message")
            t.add("Login rejected — credentials not accepted", success=False)
            return False

        await self._screenshot_step("login_unclear", "Screenshot: Login result unclear")
        t.add("Login outcome unclear (still on login page)", success=False)
        return False

    async def _login_has_error(self) -> bool:
        try:
            body = (await self.page.inner_text("body")).lower()
            return any(s in body for s in [
                "incorrect email or password", "invalid credentials",
                "wrong password", "authentication failed", "login failed",
            ])
        except Exception:
            return False

    async def _auth_register(self, data: dict, password: str, reg_url: str = _REG_URL) -> tuple[bool, str]:
        """
        Register a new portal account. Returns (success, error_code).
        Fields: firstname, mobile, email, password, rpassword
        """
        t = self.t
        t.add(f"Opening registration page: {reg_url}")
        await self._go(reg_url)
        await self._screenshot_step("reg_page", "Screenshot: Registration page loaded")

        full_name = data.get("full_name", "Applicant")
        firstname = full_name.strip().split()[0] if full_name.strip() else "Applicant"
        phone     = data.get("phone", "03001234567")
        email     = data.get("email", "")

        t.add(f"Filling registration form — name='{firstname}', email='{email}'")
        await self._fast_fill("input[name='firstname']", firstname)
        await self._fast_fill("input[name='mobile']",    phone)
        await self._fast_fill("input[name='email']",     email)
        await self._fast_fill("input[name='password']",  password)
        await self._fast_fill("input[name='rpassword']", password)

        await self._screenshot_step("reg_filled", "Screenshot: Registration form filled")
        t.add("Clicking Sign Up button")
        submitted = await self._click(["Sign Up", "Register", "Create an Account", "Create Account"])
        if not submitted:
            await self._screenshot_step("reg_no_button", "Screenshot: Sign Up button not found")
            return False, "Sign Up button not found on registration page"

        t.add("Registration submitted — waiting for response...")
        await self.page.wait_for_timeout(4000)
        body = (await self.page.inner_text("body")).lower()
        url  = self.page.url
        t.add(f"After registration: {url}")

        # Check for email-already-registered first (before success signals)
        if "already" in body and any(w in body for w in ("taken", "exists", "used", "registered", "found")):
            await self._screenshot_step("reg_already_exists", "Screenshot: Email already registered")
            t.add("Email already registered — proceeding to login", step_type="info")
            return True, "already_registered"

        # Email verification required — catch wide range of portal phrasing
        verification_signals = [
            "verify", "verification", "check your email", "confirm your email",
            "confirmation email", "activate your account", "activation link",
            "email sent", "link has been sent", "please check", "inbox",
        ]
        if any(s in body for s in verification_signals):
            await self._screenshot_step("reg_verify", "Screenshot: Email verification required")
            t.add("Email verification required before login", success=False)
            return False, "email_verification_required"

        if "account created" in body or "please login" in body or "successfully" in body:
            await self._screenshot_step("reg_success", "Screenshot: Account created successfully")
            t.add("Account created — proceeding to login")
            return True, ""

        if url.rstrip("/") != reg_url.rstrip("/"):
            await self._screenshot_step("reg_redirect", "Screenshot: Registration redirected")
            t.add("Registration complete — portal redirected")
            return True, ""

        await self._screenshot_step("reg_done", "Screenshot: Registration response")
        t.add("Registration submitted — attempting login")
        return True, ""

    # ── Full automation flow ─────────────────────────────────────────────────

    async def run_full_flow(self, portal_url: str, data: dict, uploaded_docs: dict) -> dict:
        """
        End-to-end portal automation with exact CSS selectors (live-inspected).

        Flow:
          1. Start Chromium
          2. Try login with derived credentials
          3. If login fails → register → login again
          4. Navigate to /Student/application
          5. Fill all form fields with exact selectors
          6. Submit and capture confirmation + screenshots
        """
        t           = self.t
        screenshots: list[dict] = []

        base       = portal_url.rstrip("/") if portal_url else PORTAL_URL.rstrip("/")
        login_url  = base + "/"
        reg_url    = base + "/account-registration"
        app_url    = base + "/Student/application"

        portal_email    = data.get("email", "")
        portal_password = (
            data.get("portal_password")
            or f"RIU@{data.get('cnic', 'Riphah1').replace('-', '')[:10]}"
        )
        dashboard_url = login_url  # safe default; overwritten after login

        try:
            t.add("Starting Chromium browser (visible mode)...")
            await self._start()
            t.add("Browser opened — you should see the Chromium window now")

            # ── Step 1: Open portal homepage ──────────────────────────────
            t.add(f"Opening portal: {login_url}")
            await self._go(login_url)
            await self._screenshot_step("portal_home", "Screenshot: Riphah portal homepage")

            # ── Step 2: Try login ─────────────────────────────────────────
            t.add(f"Attempting login with: {portal_email}")
            login_ok = await self._auth_login(portal_email, portal_password, login_url)

            # ── Step 3: Login failed → register → login again ─────────────
            if not login_ok:
                t.add("Login failed — will create a new portal account and try again")
                t.add(f"Creating account with email={portal_email}, password={portal_password}")

                registered, reg_msg = await self._auth_register(data, portal_password, reg_url)

                if not registered:
                    return {
                        "success":         False,
                        "screenshots":     screenshots,
                        "portal_email":    portal_email,
                        "portal_password": portal_password,
                        "dashboard_url":   dashboard_url,
                        "message": (
                            f"Could not create a portal account automatically.\n\n"
                            f"Please register manually at [{reg_url}]({reg_url}):\n"
                            f"- Name: **{data.get('full_name', '')}**\n"
                            f"- Email: **{portal_email}**\n"
                            f"- Password: **{portal_password}**"
                        ),
                    }

                if reg_msg == "email_verification_required":
                    return {
                        "success":            False,
                        "screenshots":        screenshots,
                        "portal_email":       portal_email,
                        "portal_password":    portal_password,
                        "dashboard_url":      login_url,
                        "needs_verification": True,
                        "verification_type":  "email",
                        "message": (
                            f"**Account created!** Check your inbox for a verification email.\n\n"
                            f"1. Click the link from Riphah\n"
                            f"2. Log in at [{login_url}]({login_url}) with:\n"
                            f"   - Email: **{portal_email}**  -  Password: **{portal_password}**"
                        ),
                    }

                t.add("Account created — logging in now...")
                login_ok = await self._auth_login(portal_email, portal_password, login_url)

                if not login_ok:
                    return {
                        "success":         False,
                        "screenshots":     screenshots,
                        "portal_email":    portal_email,
                        "portal_password": portal_password,
                        "dashboard_url":   login_url,
                        "message": (
                            f"Account created but login is failing.\n\n"
                            f"Log in manually at [{login_url}]({login_url}):\n"
                            f"- Email: **{portal_email}**  -  Password: **{portal_password}**"
                        ),
                    }

            # ── Step 4: Capture dashboard ─────────────────────────────────
            dashboard_url = self.page.url
            await self._screenshot_step("dashboard", "Screenshot: Dashboard after login")
            t.add(f"Dashboard ready: {dashboard_url}")

            # ── Step 5: Open Manage Applications ─────────────────────────
            t.add("Navigating to Manage Applications...")
            await self._go(app_url)
            await self.page.wait_for_timeout(1500)
            await self._screenshot_step("manage_apps", "Screenshot: Manage Applications page")
            t.add(f"Manage Applications loaded: {self.page.url}")

            # ── Step 6: Click APPLY NOW ───────────────────────────────────
            t.add("Locating APPLY NOW button...")
            clicked_apply = await self._click([
                "APPLY NOW", "Apply Now", "+ APPLY NOW",
                "BS/MS/PhD/Diploma/Certificate", "New Application",
            ])
            if clicked_apply:
                await self.page.wait_for_timeout(2500)
                await self._screenshot_step("apply_now_clicked", "Screenshot: Application form opened")
                t.add(f"Application form opened: {self.page.url}")
            else:
                t.add("APPLY NOW button not found — may already be on form", step_type="info")
                await self._screenshot_step("form_direct", "Screenshot: Direct form page")

            # ── Step 7: DOM inspection ────────────────────────────────────
            t.add("Inspecting application form — reading all fields...")
            dom = await self._dom_snapshot()

            n_inputs  = len(dom.get("inputs", []))
            n_selects = len(dom.get("selects", []))
            n_files   = len(dom.get("file_inputs", []))
            t.add(f"DOM snapshot: {n_inputs} inputs, {n_selects} dropdowns, {n_files} file inputs")

            for inp in dom.get("inputs", []):
                if inp.get("type") not in ("hidden", "submit", "button"):
                    t.add(
                        f"[FIELD] {inp.get('sel','')} | "
                        f"type={inp.get('type','')} | "
                        f"label='{inp.get('label','')}' | "
                        f"placeholder='{inp.get('placeholder','')}'",
                        step_type="info",
                    )
            for sel_el in dom.get("selects", []):
                opts_preview = ", ".join(o["text"] for o in sel_el.get("options", [])[:4])
                t.add(
                    f"[DROPDOWN] {sel_el.get('sel','')} | "
                    f"label='{sel_el.get('label','')}' | "
                    f"options: {opts_preview}...",
                    step_type="info",
                )
            for fi in dom.get("file_inputs", []):
                t.add(f"[FILE] {fi.get('sel','')} | label='{fi.get('label','')}'", step_type="info")

            # ── Step 8: Fill text fields — exact known selectors first ────
            t.add("Filling personal information...")
            full  = data.get("full_name", "")
            parts = full.strip().split()
            fname = parts[0] if len(parts) >= 1 else full
            # Explicit middle_name key wins; fall back to 3-part name split
            mname = data.get("middle_name", "") or (parts[1] if len(parts) == 3 else "")
            lname = parts[-1] if len(parts) >= 2 else ""

            known_fields = [
                ("input[name='fname']",         fname,                                       "First name"),
                ("input[name='mname']",         mname,                                       "Middle name"),
                ("input[name='lname']",         lname,                                       "Last name"),
                ("input[name='cnic']",          data.get("cnic", ""),                        "CNIC"),
                ("input[name='dob']",           data.get("dob", ""),                         "Date of birth"),
                ("input[name='fathername']",    data.get("father_name", ""),                 "Father name"),
                ("input[name='lastinstitute']", data.get("last_institute", ""),              "Last institute"),
                ("input[name='email']",         portal_email,                                "Email"),
                ("input[name='mobile']",        data.get("phone", ""),                       "Mobile"),
                ("input[name='addressline1']",  data.get("address", ""),                     "Address"),
                # Alternate No — use dedicated key, fall back to mobile
                ("input[name='phone1']",        data.get("alternate_phone",
                                                data.get("phone", "")),                      "Alternate No"),
            ]

            # Build a set of field *names* (not full selectors) so the DOM fallback
            # can skip them regardless of single-vs-double quote differences in selectors.
            known_field_names = {
                re.search(r"name=['\"]?([^'\"]+)['\"]?", row[0]).group(1)
                for row in known_fields
                if re.search(r"name=", row[0])
            }

            fields_filled = 0
            for sel_css, val, label in known_fields:
                if not val:
                    continue
                ok = await self._fast_fill(sel_css, val)
                if ok:
                    fields_filled += 1
                    t.add(f"[FILL] {sel_css} ← '{val}'", step_type="info")
                else:
                    t.add(f"[MISS] {sel_css} ({label}) — not found on this page", step_type="info")

            # Fallback: fill only inputs NOT already targeted above.
            # Uses word-boundary matching to avoid "name" matching "fname/fathername".
            for inp in dom.get("inputs", []):
                if inp.get("type") in ("hidden", "submit", "button", "file"):
                    continue
                inp_name = inp.get("name", "")
                if not inp_name or inp_name in known_field_names:
                    continue
                label_text = (
                    inp.get("label", "") + " " +
                    inp.get("placeholder", "") + " " +
                    inp_name
                ).lower()
                val = ""
                for data_key, keywords in FIELD_LABELS.items():
                    if any(
                        re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", label_text)
                        for kw in keywords
                    ):
                        val = str(data.get(data_key, "")).strip()
                        break
                if val:
                    sel_css = inp.get("sel") or f'input[name="{inp_name}"]'
                    ok = await self._fast_fill(sel_css, val)
                    if ok:
                        fields_filled += 1
                        t.add(f"[FILL] {sel_css} ← '{val}' (DOM match)", step_type="info")

            t.add(f"Text fields filled: {fields_filled}")
            await self._screenshot_step("form_personal", "Screenshot: Personal info filled")

            # ── Step 9: Fill dropdowns ─────────────────────────────────────
            t.add("Setting campus, program level, gender, nationality...")

            # --- Campus ---
            campus_map = {"islamabad": "3", "rawalpindi": "3", "lahore": "4", "malakand": "5"}
            campus_key = data.get("campus", "islamabad").lower().split()[0]
            campus_val = campus_map.get(campus_key, "3")
            try:
                await self.page.select_option("select[name='branches_id']",
                                              value=campus_val, timeout=SHORT_TIMEOUT)
                t.add(f"[SELECT] branches_id ← '{campus_val}' ({data.get('campus','Islamabad')})")
            except Exception:
                t.add("[MISS] select[name='branches_id'] — campus dropdown not found", step_type="info")
            await self.page.wait_for_timeout(1000)

            # --- Level (explicit 'level' key, fall back to deriving from program name) ---
            level_map = {
                "undergraduate": "2", "diploma": "3", "certificate": "3",
                "postgraduate": "4", "ms": "4", "mphil": "4", "mba": "4",
                "phd": "5", "doctoral": "5",
                "bs": "2", "be": "2", "bds": "2", "mbbs": "2",
            }
            level_raw  = data.get("level", data.get("program", "")).lower()
            level_val  = next((v for k, v in level_map.items() if k in level_raw), "2")
            try:
                await self.page.select_option("select[name='program_type_id']",
                                              value=level_val, timeout=SHORT_TIMEOUT)
                t.add(f"[SELECT] program_type_id ← '{level_val}' ({data.get('level','Undergraduate')})")
            except Exception:
                t.add("[MISS] select[name='program_type_id'] — level dropdown not found", step_type="info")

            # --- Programs (loaded via AJAX after campus + level are set) ---
            t.add("Waiting for program options to load (AJAX)...")
            await self.page.wait_for_timeout(2500)
            dom_prog = await self._dom_snapshot()

            def _pick_program(opts: list, name: str) -> str | None:
                if not name:
                    return None
                n = name.lower()
                return next(
                    (o["value"] for o in opts
                     if n in o["text"].lower() or o["text"].lower() in n),
                    None,
                )

            prog_slots = [
                ("programs_id",  data.get("program",  "")),
                ("programs_id2", data.get("program2", "")),
                ("programs_id3", data.get("program3", "")),
                ("programs_id4", data.get("program4", "")),
            ]
            # Build a lookup: name → options list from fresh DOM snapshot
            prog_opts_by_name = {
                s.get("name"): s.get("options", [])
                for s in dom_prog.get("selects", [])
                if s.get("name", "").startswith("programs_id")
            }
            for slot_name, prog_name in prog_slots:
                if not prog_name:
                    continue
                opts = prog_opts_by_name.get(slot_name, [])
                match_val = _pick_program(opts, prog_name)
                if match_val:
                    try:
                        await self.page.select_option(
                            f"select[name='{slot_name}']", value=match_val, timeout=SHORT_TIMEOUT)
                        t.add(f"[SELECT] {slot_name} ← '{match_val}' ({prog_name})")
                    except Exception:
                        pass
                elif prog_name:
                    t.add(f"[WARN] '{prog_name}' not found in {slot_name} options", step_type="info")

            # --- Gender ---
            gender_val = "Female" if "female" in data.get("gender", "").lower() else "Male"
            try:
                await self.page.select_option("select[name='gender']",
                                              value=gender_val, timeout=SHORT_TIMEOUT)
                t.add(f"[SELECT] gender ← '{gender_val}'")
            except Exception:
                pass

            # --- Nationality ---
            try:
                await self.page.select_option("select[name='nationality']",
                                              label=data.get("nationality", "Pakistan"),
                                              timeout=SHORT_TIMEOUT)
                t.add(f"[SELECT] nationality ← '{data.get('nationality','Pakistan')}'")
            except Exception:
                pass

            # --- City (dedicated 'city' key, not extracted from address string) ---
            city = data.get("city", "") or data.get("address", "").strip().split(",")[0].strip()
            if city:
                try:
                    await self.page.select_option("select[name='city1']", label=city,
                                                  timeout=SHORT_TIMEOUT)
                    t.add(f"[SELECT] city1 ← '{city}'")
                except Exception:
                    pass

            # --- How did you hear about us ---
            heard = data.get("heard_from", "Friend or Family")
            try:
                await self.page.select_option("select[name='aboutus']",
                                              label=heard, timeout=SHORT_TIMEOUT)
                t.add(f"[SELECT] aboutus ← '{heard}'")
            except Exception:
                pass

            t.add("Dropdowns set")
            await self._screenshot_step("form_dropdowns", "Screenshot: Dropdowns filled")

            # ── Step 10: Pre-submit full form screenshot ───────────────────
            await self._screenshot_step("form_filled", "Screenshot: Application form complete (ready to submit)")

            # ── Step 11: Upload documents (if any) ────────────────────────
            if uploaded_docs:
                t.add(f"Uploading {len(uploaded_docs)} document(s)...")
                fresh_dom = await self._dom_snapshot()
                doc_kw = {
                    "cnic":   ["cnic", "id card", "national id"],
                    "photo":  ["photo", "picture", "photograph", "passport"],
                    "matric": ["matric", "ssc", "secondary"],
                    "inter":  ["intermediate", "hssc", "inter"],
                }
                for doc_type, doc_path in uploaded_docs.items():
                    if not doc_path:
                        continue
                    for fi in fresh_dom.get("file_inputs", []):
                        fi_text = (fi.get("label", "") + " " + fi.get("name", "")).lower()
                        if any(kw in fi_text for kw in doc_kw.get(doc_type, [doc_type])):
                            try:
                                await self.page.locator(fi["sel"]).set_input_files(
                                    doc_path, timeout=SHORT_TIMEOUT)
                                t.add(f"[UPLOAD] {doc_type} uploaded to {fi['sel']}")
                            except Exception:
                                pass

            # ── Step 12: Submit ────────────────────────────────────────────
            t.add("Submitting application form...")
            submit_ok = await self._click(["SUBMIT", "Submit Application", "Submit", "Proceed"])
            if not submit_ok:
                try:
                    await self.page.locator('[type="submit"]').first.click(timeout=SHORT_TIMEOUT)
                    submit_ok = True
                except Exception:
                    pass
            if submit_ok:
                await self.page.wait_for_timeout(5000)
                t.add("Form submitted — waiting for portal response...")

            # ── Step 13: Capture confirmation ─────────────────────────────
            await self._screenshot_step("confirmation", "Screenshot: Submission confirmation page")

            body     = await self.page.inner_text("body")
            body_low = body.lower()
            ref_m    = re.search(
                r'(?:application|reference|tracking|id)[\s:=#]*([A-Z0-9\-]{4,25})',
                body, re.IGNORECASE)
            reference = ref_m.group(1).strip() if ref_m else None
            if reference:
                t.add(f"Reference number: {reference}")

            page_ok   = any(s in body_low for s in [
                "success", "submitted", "received", "thank you",
                "congratulation", "application id", "manage application"])
            page_fail = any(s in body_low for s in [
                "incorrect email", "invalid credentials", "login required"])
            is_success = submit_ok and page_ok and not page_fail

            if is_success:
                t.add("Application submitted successfully!")
                msg = (
                    f"**Application submitted successfully!**\n\n"
                    + (f"- **Reference:** `{reference}`\n" if reference else "")
                    + f"- **Email:** {portal_email}\n"
                    f"- **Password:** {portal_password}\n\n"
                    f"Log in at [{dashboard_url}]({dashboard_url}) to check your status."
                )
            elif submit_ok and not page_fail:
                t.add("Form submitted — portal response unclear", step_type="info")
                msg = (
                    f"Form was submitted but the portal response was unclear.\n\n"
                    f"Log in at [{dashboard_url}]({dashboard_url}) to verify:\n"
                    f"- Email: **{portal_email}**  -  Password: **{portal_password}**"
                )
            else:
                t.add("Submission incomplete — check screenshot for details", success=False)
                msg = (
                    f"The form could not be submitted automatically.\n\n"
                    f"Complete it manually at [{app_url}]({app_url})\n"
                    f"- Email: **{portal_email}**  -  Password: **{portal_password}**"
                )

            return {
                "success":         is_success or (submit_ok and not page_fail),
                "reference":       reference,
                "message":         msg,
                "screenshots":     screenshots,
                "portal_email":    portal_email,
                "portal_password": portal_password,
                "dashboard_url":   dashboard_url,
            }

        except Exception as e:
            import traceback
            err_class = type(e).__name__
            err_msg   = str(e).strip() or repr(e)
            safe_err  = f"[PortalAgent] ERROR {err_class}: {err_msg}".encode("ascii", errors="replace").decode("ascii")
            print(safe_err, flush=True)
            traceback.print_exc()

            hint = ""
            low = (err_msg + " " + err_class).lower()
            if "executable" in low or "browsertype" in low or "playwright install" in low:
                hint = "\n\nRun: `python -m playwright install chromium`"
            elif "timeout" in low or "net::err" in low:
                hint = "\n\nCheck internet connection — portal may be unreachable."
            elif "notimplementederror" in low:
                hint = "\n\nRestart the backend (ProactorEventLoop required on Windows)."

            t.add(f"ERROR: {err_class} — {err_msg}", success=False, step_type="error")
            try:
                await self._screenshot_step("error_state", "Screenshot: Error state")
            except Exception:
                pass

            return {
                "success":         False,
                "message":         f"Automation failed — **{err_class}**: {err_msg}{hint}",
                "screenshots":     screenshots,
                "portal_email":    portal_email,
                "portal_password": portal_password,
                "dashboard_url":   dashboard_url,
            }

        finally:
            await self._stop()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

async def scan_portal_async(url: str = PORTAL_URL) -> dict:
    tracker = PortalProgressTracker()
    return await AdmissionPortalAgent(tracker).scan_portal(url)


def scan_portal_sync_with_timeout(url: str = PORTAL_URL, timeout_seconds: int = 14) -> dict:
    """Run async portal scan in a daemon thread with a hard timeout."""
    result: dict = {}

    def _run():
        try:
            result.update(asyncio.run(scan_portal_async(url)))
        except Exception as e:
            print(f"[PortalAgent] Scan thread error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)

    return result if result.get("fields") else {
        "fields": DEFAULT_FIELDS,
        "required_documents": ["cnic", "photo", "matric_certificate", "inter_certificate"],
        "has_signup": True,
        "scan_failed": True,
    }


async def run_automation_async(
    data: dict,
    uploaded_docs: dict,
    progress_queue,
    portal_url: str = PORTAL_URL,
) -> dict:
    """Async entry point for full portal automation, streaming steps to progress_queue.

    `progress_queue` only needs to expose `put_nowait` — works with both
    `asyncio.Queue` and the thread-safe bridge defined in `main.py`.
    The caller is responsible for sending the final `done` signal.
    """
    tracker = PortalProgressTracker()
    tracker.add_queue(progress_queue)
    agent = AdmissionPortalAgent(tracker)
    return await agent.run_full_flow(portal_url, data, uploaded_docs)
