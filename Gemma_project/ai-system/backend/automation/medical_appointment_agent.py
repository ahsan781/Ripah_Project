"""
Medical Appointment Automation — RMC Riphah Medical Centre
===========================================================
Target:  https://rmc.riphah.edu.pk/appointment/
Engine:  MetForm v4.0.2 (WordPress/Elementor plugin)
Submit:  POST https://rmc.riphah.edu.pk/wp-json/metform/v1/entries/insert/2980

No login required — the form is publicly accessible.

Form fields (live-inspected from MetForm preact template):

  Field name        | Type                   | CSS selector / notes
  ──────────────────┼────────────────────────┼──────────────────────────────────────
  booking_doctor    | React-Select dropdown  | .elementor-element-79f8371 .mf-input-select__control
                    |                        | Options are divs (.mf-input-select__option), NOT <option>
  booking_name      | text input             | input[name="booking_name"]   (#mf-input-text-acdc13f)
  booking_age       | number input           | input[name="booking_age"]    (#mf-input-mobile-12a4ae5)
  booking_phone     | number input           | input[name="booking_phone"]  (#mf-input-mobile-1d6d015)
  booking_email     | email input            | input[name="booking_email"]  (#mf-input-email-f869dd6)
  booking_date      | Flatpickr date picker  | input[name="booking_date"]   (format: m-d-Y, minDate today)
  booking_time      | Flatpickr time picker  | input[name="booking_time"]   (format: h:i K, 12-hr AM/PM)
  booking_message   | textarea               | textarea[name="booking_message"] (#mf-input-text-area-6435bdc)

Submit button:  button.metform-submit-btn  ("Make an Appointment")
Success signal: cute-alert.js popup (no page redirect — MetForm uses fetch() REST API)
"""

import asyncio
import os
import re
import time
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

APPOINTMENT_URL = os.getenv("RIPHAH_APPOINTMENT_URL", "https://rmc.riphah.edu.pk/appointment/")

SCREENSHOTS_DIR = Path(__file__).parent.parent / "static" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

SHORT_TIMEOUT  = 5_000   # ms — per-locator timeout
NAV_TIMEOUT    = 30_000  # ms — page.goto() timeout
REACT_WAIT     = 6_000   # ms — extra wait for React-Select / Flatpickr to mount

# Exact doctor names as they appear in the React-Select options
# (values equal label text in this MetForm configuration)
DOCTOR_OPTIONS = [
    "Ms. Sidrah Kanwal",
    "Ms. Inaba Shujaat Qureshi",
    "Saira Khalid",
    "Dr. Farheen Naz Anis",
    "Dr Muhammad Hashim PT",
    "Dr Mehar un nisa PT",
    "Dr.Iqra Abdul Ghafoor",
    "Ms. Rimsha Tufail",
    "Dr. Arooj Arshad",
    "Ms. Aleena Arshad",
    "Ms Sadaf Rehman",
    "Dr. Ammar Hameed PT",
]

# Fallback field list returned when the portal is unreachable
DEFAULT_FIELDS = [
    {
        "key": "doctor", "label": "Select Doctor", "type": "select",
        "required": True, "options": DOCTOR_OPTIONS,
    },
    {"key": "patient_name", "label": "Patient Full Name",            "type": "text",     "required": True},
    {"key": "age",          "label": "Patient Age",                   "type": "number",   "required": True},
    {"key": "phone",        "label": "Phone Number",                  "type": "phone",    "required": True},
    {"key": "email",        "label": "Email Address",                 "type": "email",    "required": True},
    {"key": "date",         "label": "Appointment Date (MM-DD-YYYY)", "type": "date",     "required": True},
    {"key": "time_slot",    "label": "Appointment Time (e.g. 10:30 AM)", "type": "text",  "required": True},
    {"key": "message",      "label": "Symptoms / Reason for Visit",   "type": "textarea", "required": True},
]


# ── Progress tracker (identical interface to PortalProgressTracker) ──────────

class AppointmentProgressTracker:
    def __init__(self):
        self.steps: list[dict] = []
        self._queues: list = []

    def add_queue(self, q):
        self._queues.append(q)

    def add(
        self,
        message: str,
        success: bool = True,
        step_type: str = "step",
        extra: dict | None = None,
    ):
        step = {
            "message":   message,
            "success":   success,
            "type":      step_type,
            "timestamp": time.time(),
        }
        if extra:
            step.update(extra)
        self.steps.append(step)
        for q in self._queues:
            try:
                q.put_nowait(step)
            except Exception:
                pass
        icon = "[OK]" if success else "[FAIL]"
        safe = (
            f"[MedicalAgent] {icon} {message}"
            .encode("ascii", errors="replace")
            .decode("ascii")
        )
        print(safe, flush=True)


# ── Core agent ────────────────────────────────────────────────────────────────

class MedicalAppointmentAgent:
    """
    Playwright automation for the Riphah Medical Centre appointment form.

    Key differences from the admission portal agent:

    1. DOCTOR SELECTION — React-Select component (not a native <select>).
       We must click the .mf-input-select__control div, wait for the dropdown
       menu to open, then click the matching .mf-input-select__option div.
       Native page.select_option() does NOT work here.

    2. DATE / TIME — Flatpickr widgets. Standard .fill() is ignored because
       Flatpickr intercepts keyboard input and manages its own state.
       We inject the value via el._flatpickr.setDate() using page.evaluate(),
       then fall back to forcing the value through the native input setter and
       dispatching synthetic events so MetForm's React layer picks them up.

    3. SUBMISSION — MetForm posts via fetch() to its REST endpoint; no page
       navigation occurs on success. We wait for the cute-alert.js popup (or
       any success text in the DOM) to appear after the submit click.
    """

    def __init__(self, tracker: AppointmentProgressTracker):
        self.t        = tracker
        self._pw      = None
        self._browser = None
        self.page     = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _start(self):
        from playwright.async_api import async_playwright
        self._pw      = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=False,
            slow_mo=350,
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
        self.page.set_default_timeout(SHORT_TIMEOUT)

    async def _stop(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ── Navigation ───────────────────────────────────────────────────────────

    async def _go(self, url: str):
        await self.page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(800)

    # ── DOM snapshot ─────────────────────────────────────────────────────────

    async def _dom_snapshot(self) -> dict:
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
                        options:Array.from(e.options).slice(0,40).map(o=>({value:o.value,text:o.text.trim()}))
                    })),
                    textareas: Array.from(document.querySelectorAll('textarea')).filter(vis).map(e=>({
                        sel:sel(e), name:e.name, id:e.id, label:lbl(e), placeholder:e.placeholder
                    })),
                    buttons: Array.from(document.querySelectorAll(
                        'button,[type=submit],[role=button]'
                    )).filter(vis).map(e=>({
                        text:e.innerText.trim().slice(0,80), id:e.id, type:e.getAttribute('type')
                    })),
                };
            }""")
        except Exception:
            return {
                "url": "", "title": "", "inputs": [], "selects": [],
                "textareas": [], "buttons": [],
            }

    # ── Low-level interaction helpers ────────────────────────────────────────

    async def _fast_fill(self, selector: str, value: str) -> bool:
        """Fill one CSS selector with SHORT_TIMEOUT. Returns True on success."""
        if not selector or not value:
            return False
        try:
            await self.page.locator(selector).fill(str(value), timeout=SHORT_TIMEOUT)
            return True
        except Exception:
            return False

    async def _click(self, candidates: list[str]) -> bool:
        """Click first matching button/link/text. Returns True if any matched."""
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

    # ── Screenshot helpers ───────────────────────────────────────────────────

    async def _screenshot(self, label: str) -> str:
        filename = f"rmc_{label}_{int(time.time())}.png"
        path = str(SCREENSHOTS_DIR / filename)
        try:
            await self.page.screenshot(path=path, full_page=False, timeout=8000)
            return path
        except Exception:
            return ""

    async def _screenshot_step(self, label: str, caption: str) -> str:
        """Take screenshot AND emit a 'screenshot' tracker step."""
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

    # ── React-Select doctor dropdown ─────────────────────────────────────────

    async def _select_react_doctor(self, doctor_name: str) -> bool:
        """
        Select a doctor from the MetForm React-Select dropdown.

        React-Select renders its option list as a portal appended to <body>,
        so options may NOT be inside the Elementor wrapper. We use broad
        document-level selectors after opening the menu.
        """
        t = self.t
        if not doctor_name:
            return False

        t.add(f"Selecting doctor: '{doctor_name}'", step_type="info")

        # ── Step 1: Scroll the control into view and click to open ───────────
        control_selectors = [
            ".elementor-element-79f8371 .mf-input-select__control",
            ".mf-input-select__control",
            "[id^='mf-input-select']",
        ]
        opened = False
        for sel in control_selectors:
            try:
                loc = self.page.locator(sel).first
                await loc.scroll_into_view_if_needed(timeout=SHORT_TIMEOUT)
                await loc.click(timeout=SHORT_TIMEOUT)
                await self.page.wait_for_timeout(1000)
                opened = True
                t.add(f"[CLICK] Opened dropdown via '{sel}'", step_type="info")
                break
            except Exception:
                continue

        if not opened:
            t.add("[WARN] Could not click React-Select control — trying JS open", step_type="info")
            # Force open via JS: simulate click on the first mf-input-select__control
            try:
                await self.page.evaluate("""() => {
                    const ctrl = document.querySelector('.mf-input-select__control');
                    if (ctrl) ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                }""")
                await self.page.wait_for_timeout(1000)
            except Exception:
                pass

        # ── Step 2: Wait for the option list to appear (portal or inline) ───
        try:
            await self.page.locator(".mf-input-select__option").first.wait_for(
                state="visible", timeout=4000
            )
        except Exception:
            t.add("[WARN] Option list not visible yet — trying anyway", step_type="info")

        # ── Step 3: Click the matching option (document-level search) ────────
        # Try exact name first, then first-word partial match
        for attempt_name in [doctor_name, doctor_name.split()[0], doctor_name.split()[-1]]:
            try:
                # All .mf-input-select__option divs anywhere in document
                opt = self.page.locator(".mf-input-select__option").filter(has_text=attempt_name)
                count = await opt.count()
                if count > 0:
                    await opt.first.scroll_into_view_if_needed(timeout=2000)
                    await opt.first.click(timeout=SHORT_TIMEOUT)
                    await self.page.wait_for_timeout(600)
                    t.add(f"[SELECT] Doctor <- '{doctor_name}' (option click, matched '{attempt_name}')")
                    return True
            except Exception:
                continue

        # ── Step 4: get_by_role / get_by_text broad fallback ─────────────────
        try:
            await self.page.get_by_text(doctor_name, exact=False).first.click(timeout=SHORT_TIMEOUT)
            await self.page.wait_for_timeout(600)
            t.add(f"[SELECT] Doctor <- '{doctor_name}' (get_by_text fallback)")
            return True
        except Exception:
            pass

        # ── Step 5: JS injection — directly set the hidden input & fire events
        try:
            ok = await self.page.evaluate(
                """(val) => {
                    // Try to set the hidden booking_doctor input
                    const hidden = document.querySelector('input[name="booking_doctor"]');
                    if (hidden) {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(hidden, val);
                        hidden.dispatchEvent(new Event('input',  {bubbles:true}));
                        hidden.dispatchEvent(new Event('change', {bubbles:true}));
                    }
                    // Also try to click the matching option if menu is still open
                    const opts = document.querySelectorAll('.mf-input-select__option');
                    for (const o of opts) {
                        if (o.textContent.includes(val)) { o.click(); return true; }
                    }
                    return !!hidden;
                }""",
                doctor_name,
            )
            if ok:
                await self.page.wait_for_timeout(600)
                t.add(f"[SELECT] Doctor <- '{doctor_name}' (JS injection)")
                return True
        except Exception:
            pass

        t.add(f"[FAIL] Could not select doctor '{doctor_name}'", success=False)
        return False

    def _pick_best_doctor(self, hint: str) -> str:
        """
        Given a free-text hint (name, specialty, etc.) return the closest
        DOCTOR_OPTIONS entry. Falls back to the first option.
        """
        if not hint:
            return DOCTOR_OPTIONS[0]
        h = hint.lower()
        # Exact match
        for opt in DOCTOR_OPTIONS:
            if opt.lower() == h:
                return opt
        # Substring match
        for opt in DOCTOR_OPTIONS:
            if h in opt.lower() or opt.lower() in h:
                return opt
        # Token overlap
        h_tokens = set(h.split())
        best, best_score = DOCTOR_OPTIONS[0], 0
        for opt in DOCTOR_OPTIONS:
            score = len(h_tokens & set(opt.lower().split()))
            if score > best_score:
                best, best_score = opt, score
        return best

    # ── Flatpickr date / time helpers ────────────────────────────────────────

    async def _set_flatpickr_field(self, input_name: str, value: str, label: str) -> bool:
        """
        Set a Flatpickr-controlled input field.

        Flatpickr intercepts all keyboard input on the visible <input> and
        manages its own state object. Simply calling .fill() on the input
        updates the DOM value but Flatpickr ignores it — the form submission
        will still read from Flatpickr's internal state (which remains unset).

        Three-stage approach:
          1. el._flatpickr.setDate(value, true)  — official Flatpickr API
          2. Force-set via native input setter + dispatchEvent  — triggers
             MetForm's React onChange which reads input.value directly
          3. Plain .fill() as a last resort
        """
        t   = self.t
        sel = f'input[name="{input_name}"]'

        t.add(f"Setting {label} '{value}' via Flatpickr...", step_type="info")

        # Open the picker so Flatpickr initialises if deferred
        try:
            await self.page.locator(sel).click(timeout=SHORT_TIMEOUT)
            await self.page.wait_for_timeout(500)
        except Exception:
            pass

        # Stage 1: Flatpickr JS instance API
        try:
            ok = await self.page.evaluate(
                """([name, val]) => {
                    const el = document.querySelector('input[name="' + name + '"]');
                    if (el && el._flatpickr) {
                        el._flatpickr.setDate(val, true);
                        return true;
                    }
                    return false;
                }""",
                [input_name, value],
            )
            if ok:
                await self.page.wait_for_timeout(300)
                await self.page.keyboard.press("Escape")
                t.add(f"[FILL] {sel} <- '{value}' (Flatpickr .setDate())")
                return True
        except Exception:
            pass

        # Stage 2: native input setter + synthetic events
        try:
            await self.page.evaluate(
                """([name, val]) => {
                    const el = document.querySelector('input[name="' + name + '"]');
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input',  { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                [input_name, value],
            )
            await self.page.wait_for_timeout(300)
            await self.page.keyboard.press("Escape")
            t.add(f"[FILL] {sel} <- '{value}' (JS value injection)")
            return True
        except Exception:
            pass

        # Stage 3: plain fill fallback
        ok = await self._fast_fill(sel, value)
        if ok:
            await self.page.keyboard.press("Escape")
            t.add(f"[FILL] {sel} <- '{value}' (plain fill fallback)")
        return ok

    # ── Date / time normalisation ────────────────────────────────────────────

    @staticmethod
    def _normalise_date(raw: str) -> str:
        """
        Return date in MM-DD-YYYY format (Flatpickr dateFormat "m-d-Y").

        Accepts:
          "2026-06-15"   ISO 8601     → "06-15-2026"
          "15/06/2026"   DD/MM/YYYY  → "06-15-2026"
          "06/15/2026"   MM/DD/YYYY  → "06-15-2026" (passthrough)
          "06-15-2026"   already OK  → "06-15-2026"
        """
        raw = raw.strip()
        # YYYY-MM-DD
        m = re.fullmatch(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', raw)
        if m:
            return f"{int(m.group(2)):02d}-{int(m.group(3)):02d}-{m.group(1)}"
        # DD/MM/YYYY or MM/DD/YYYY or MM-DD-YYYY
        m = re.fullmatch(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', raw)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
            # If first part > 12 it must be DD
            if a > 12:
                return f"{b:02d}-{a:02d}-{y}"
            # Otherwise treat as MM-DD-YYYY (portal format)
            return f"{a:02d}-{b:02d}-{y}"
        return raw  # cannot parse — pass through as-is

    @staticmethod
    def _normalise_time(raw: str) -> str:
        """
        Return time in h:i K format (Flatpickr dateFormat "h:i K", 12-hour AM/PM).

        Accepts:
          "10:30 AM"  → "10:30 AM"
          "14:30"     → "2:30 PM"
          "1430"      → "2:30 PM"
          "2:30PM"    → "2:30 PM"
        """
        raw = raw.strip().upper()
        # Already has AM/PM — normalise spacing
        if re.search(r'[AP]M$', raw):
            raw = re.sub(r'(\d)(AM|PM)', r'\1 \2', raw)
            return raw
        # 24-hour HH:MM or HHMM
        m = re.fullmatch(r'(\d{1,2}):?(\d{2})', raw)
        if m:
            h, mi  = int(m.group(1)), m.group(2)
            period = "AM" if h < 12 else "PM"
            h12    = h % 12 or 12
            return f"{h12}:{mi} {period}"
        return raw

    # ── Full booking flow ─────────────────────────────────────────────────────

    async def run_full_flow(self, appointment_url: str, data: dict) -> dict:
        """
        End-to-end Riphah Medical Centre appointment booking.

        Data dict keys accepted (with aliases):
          patient_name / full_name / name  — patient full name
          age / patient_age               — patient age (integer or string)
          phone / mobile                  — contact phone number
          email                           — email address
          doctor / specialty              — doctor name (matched against DOCTOR_OPTIONS)
          date / appointment_date         — date in any parseable format
          time_slot / time                — time in any parseable format
          message / symptoms / reason     — reason for visit / message

        Returns dict with keys:
          success (bool), message (str markdown), screenshots (list),
          doctor (str), date (str), time (str), patient (str), email (str)
        """
        t    = self.t
        screenshots: list[dict] = []
        url  = (appointment_url or APPOINTMENT_URL).rstrip("/") + "/"

        # ── Resolve input data ────────────────────────────────────────────────
        patient_name = (
            data.get("patient_name") or data.get("full_name") or data.get("name") or ""
        ).strip()
        age          = str(data.get("age", data.get("patient_age", ""))).strip()
        phone        = str(data.get("phone", data.get("mobile", ""))).strip()
        email        = str(data.get("email", "")).strip()
        doctor_hint  = str(data.get("doctor", data.get("specialty", ""))).strip()
        raw_date     = str(data.get("date", data.get("appointment_date", ""))).strip()
        raw_time     = str(data.get("time_slot", data.get("time", ""))).strip()
        message      = str(
            data.get("message") or data.get("symptoms") or data.get("reason") or
            "Appointment booking via automated system."
        ).strip()

        # Normalise date / time to Flatpickr formats
        date_str = self._normalise_date(raw_date) if raw_date else ""
        time_str = self._normalise_time(raw_time) if raw_time else ""

        # Best-match doctor from the known list
        doctor = self._pick_best_doctor(doctor_hint)

        try:
            t.add("Starting Chromium browser (visible mode)...")
            await self._start()
            t.add("Browser opened")

            # ── Step 1: Navigate ──────────────────────────────────────────
            t.add(f"Opening appointment page: {url}")
            await self._go(url)
            await self._screenshot_step("page_loaded", "Screenshot: Appointment page loaded")

            # ── Step 2: Wait for MetForm / React-Select to mount ──────────
            t.add("Waiting for MetForm widgets (React-Select, Flatpickr) to initialise...")
            await self.page.wait_for_timeout(REACT_WAIT)
            try:
                await self.page.locator(".metform-form-content").wait_for(
                    state="visible", timeout=8000
                )
                t.add("MetForm form confirmed visible")
            except Exception:
                t.add("[WARN] .metform-form-content not found — proceeding", step_type="info")

            # Scroll form into viewport so all elements are interactable
            try:
                await self.page.locator(".metform-form-content").scroll_into_view_if_needed(timeout=3000)
                await self.page.wait_for_timeout(500)
            except Exception:
                pass

            await self._screenshot_step("form_ready", "Screenshot: Form ready")

            # ── Step 3: DOM inspection ─────────────────────────────────────
            dom = await self._dom_snapshot()
            t.add(
                f"DOM snapshot: {len(dom.get('inputs',[]))} inputs, "
                f"{len(dom.get('textareas',[]))} textareas",
                step_type="info",
            )

            # ── Step 4: Select doctor (React-Select) ─────────────────────
            t.add(f"Selecting doctor: {doctor}")
            doctor_ok = await self._select_react_doctor(doctor)
            if not doctor_ok:
                t.add(f"[WARN] Doctor selection uncertain for '{doctor}'", step_type="info")
            await self._screenshot_step("doctor_selected", f"Screenshot: Doctor '{doctor}' selected")

            # ── Step 5: Fill text / number / email inputs ─────────────────
            t.add("Filling patient details...")
            # Exact selectors from live DOM inspection
            text_fields = [
                ('input[name="booking_name"]',  patient_name, "Patient name"),
                ('#mf-input-text-acdc13f',       patient_name, "Patient name (ID fallback)"),
                ('input[name="booking_age"]',   age,          "Age"),
                ('#mf-input-mobile-12a4ae5',     age,          "Age (ID fallback)"),
                ('input[name="booking_phone"]', phone,        "Phone"),
                ('#mf-input-mobile-1d6d015',     phone,        "Phone (ID fallback)"),
                ('input[name="booking_email"]', email,        "Email"),
                ('#mf-input-email-f869dd6',      email,        "Email (ID fallback)"),
            ]
            # Track which field names we have already successfully filled
            filled_names: set[str] = set()
            fields_filled = 0
            for css, val, label in text_fields:
                if not val:
                    continue
                # Extract the field name from the selector to avoid re-filling
                name_match = re.search(r'name="([^"]+)"', css)
                field_name = name_match.group(1) if name_match else css
                if field_name in filled_names:
                    continue  # already successfully filled via primary selector
                ok = await self._fast_fill(css, val)
                if ok:
                    fields_filled += 1
                    filled_names.add(field_name)
                    t.add(f"[FILL] {css} <- '{val}'", step_type="info")

            t.add(f"Text fields filled: {fields_filled}")
            await self._screenshot_step("text_filled", "Screenshot: Text fields filled")

            # ── Step 6: Set appointment date (Flatpickr) ──────────────────
            if date_str:
                await self._set_flatpickr_field("booking_date", date_str, "appointment date")
            else:
                t.add("[SKIP] No appointment date provided", step_type="info")

            # ── Step 7: Set appointment time (Flatpickr) ──────────────────
            if time_str:
                await self._set_flatpickr_field("booking_time", time_str, "appointment time")
            else:
                t.add("[SKIP] No appointment time provided", step_type="info")

            await self._screenshot_step(
                "datetime_filled",
                f"Screenshot: Date '{date_str}' / Time '{time_str}' set",
            )

            # ── Step 8: Fill message textarea ─────────────────────────────
            msg_filled = await self._fast_fill('textarea[name="booking_message"]', message)
            if not msg_filled:
                # ID fallback
                msg_filled = await self._fast_fill("#mf-input-text-area-6435bdc", message)
            if msg_filled:
                t.add(f"[FILL] booking_message <- '{message[:60]}...'", step_type="info")
            else:
                t.add("[MISS] Message textarea not found", step_type="info")

            # ── Step 9: Pre-submit screenshot ─────────────────────────────
            await self._screenshot_step(
                "form_complete",
                "Screenshot: Form complete — ready to submit",
            )

            # ── Step 10: Submit ───────────────────────────────────────────
            t.add("Submitting appointment form...")
            submit_ok = False

            # Primary: exact submit button class (MetForm renders button.metform-submit-btn)
            try:
                await self.page.locator("button.metform-submit-btn").click(timeout=SHORT_TIMEOUT)
                submit_ok = True
                t.add("Clicked button.metform-submit-btn")
            except Exception:
                pass

            # Secondary: by button text
            if not submit_ok:
                submit_ok = await self._click([
                    "Make an Appointment",
                    "Book Appointment",
                    "Submit",
                    "Book Now",
                ])

            # Tertiary: any submit input/button
            if not submit_ok:
                try:
                    await self.page.locator('[type="submit"]').first.click(timeout=SHORT_TIMEOUT)
                    submit_ok = True
                    t.add("Clicked [type=submit] fallback")
                except Exception:
                    pass

            if submit_ok:
                # MetForm submits via fetch() — no navigation, wait for alert popup
                t.add("Form submitted — waiting for MetForm REST response...")
                await self.page.wait_for_timeout(5000)
            else:
                t.add("[FAIL] Submit button not found", success=False)

            # ── Step 11: Detect success ────────────────────────────────────
            await self._screenshot_step("confirmation", "Screenshot: Submission response")

            body_low = (await self.page.inner_text("body")).lower()

            # Also check the cute-alert popup (MetForm's alert library)
            try:
                alert_text = await self.page.locator(".cute-alert").inner_text(timeout=2000)
                body_low += " " + alert_text.lower()
            except Exception:
                pass
            # Check MetForm's own response container
            try:
                resp_text = await self.page.locator(".mf-form-response").inner_text(timeout=2000)
                body_low += " " + resp_text.lower()
            except Exception:
                pass

            success_signals = [
                "thank you", "thanks", "success", "submitted", "received",
                "appointment confirmed", "booking received", "your appointment",
                "we will contact", "has been submitted",
            ]
            error_signals = [
                "error", "failed", "invalid", "required field",
                "please fill", "fill in all", "not valid",
            ]

            page_ok   = any(s in body_low for s in success_signals)
            page_fail = any(s in body_low for s in error_signals)
            is_success = submit_ok and page_ok and not page_fail

            if is_success:
                t.add("Appointment booked successfully!")
                msg = (
                    f"**Appointment booking submitted successfully!**\n\n"
                    f"- **Patient:** {patient_name}\n"
                    f"- **Doctor:** {doctor}\n"
                    f"- **Date:** {date_str or '(as selected)'}\n"
                    f"- **Time:** {time_str or '(as selected)'}\n"
                    f"- **Phone:** {phone}\n"
                    f"- **Email:** {email}\n\n"
                    f"Please check your email or phone for a confirmation from the clinic."
                )
            elif submit_ok and not page_fail:
                t.add("Form submitted — portal response unclear", step_type="info")
                msg = (
                    f"Appointment form was submitted but the portal response was unclear.\n\n"
                    f"- Patient: **{patient_name}** | Doctor: **{doctor}**\n"
                    f"- Date: **{date_str}** | Time: **{time_str}**\n\n"
                    f"Please verify at [{url}]({url}) or call the clinic directly."
                )
            else:
                t.add("Submission incomplete — check screenshot for details", success=False)
                msg = (
                    f"The appointment form could not be submitted automatically.\n\n"
                    f"Please complete it manually at [{url}]({url}):\n"
                    f"- Patient: **{patient_name}**\n"
                    f"- Doctor: **{doctor}**\n"
                    f"- Date: **{date_str}** | Time: **{time_str}**"
                )

            return {
                "success":     is_success or (submit_ok and not page_fail),
                "message":     msg,
                "screenshots": screenshots,
                "doctor":      doctor,
                "date":        date_str,
                "time":        time_str,
                "patient":     patient_name,
                "email":       email,
            }

        except Exception as e:
            import traceback
            err_class = type(e).__name__
            err_msg   = str(e).strip() or repr(e)
            safe_err  = (
                f"[MedicalAgent] ERROR {err_class}: {err_msg}"
                .encode("ascii", errors="replace")
                .decode("ascii")
            )
            print(safe_err, flush=True)
            traceback.print_exc()

            hint = ""
            low  = (err_msg + " " + err_class).lower()
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
                "success":     False,
                "message":     f"Automation failed — **{err_class}**: {err_msg}{hint}",
                "screenshots": screenshots,
                "doctor":      "",
                "date":        "",
                "time":        "",
                "patient":     "",
                "email":       "",
            }

        finally:
            await self._stop()


# ── Module-level entry point (mirrors run_automation_async from portal_agent.py) ─

async def run_appointment_async(
    data: dict,
    progress_queue,
    appointment_url: str = APPOINTMENT_URL,
) -> dict:
    """
    Async entry point for full appointment booking automation.
    Streams progress steps to progress_queue (needs only put_nowait).

    Returns dict with keys:
      success (bool), message (str markdown), screenshots (list),
      doctor (str), date (str), time (str), patient (str), email (str)
    """
    tracker = AppointmentProgressTracker()
    tracker.add_queue(progress_queue)
    agent = MedicalAppointmentAgent(tracker)
    return await agent.run_full_flow(appointment_url, data)
