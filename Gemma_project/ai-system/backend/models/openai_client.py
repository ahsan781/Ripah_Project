"""
Ollama/Gemma drop-in replacement for the original OpenAI client.

Uses Ollama's OpenAI-compatible REST endpoint (http://ollama:11434/v1) so
the openai SDK keeps working — only the base_url and api_key change.

All public names are identical to the original so every import in the
codebase works without modification:
  generate, generate_json, chat_with_history, embed,
  health_check, pull_model, ensure_models,
  WORKFLOW_MODEL, INTENT_MODEL, AGENT_MODEL, EMBED_MODEL

Local model stack (default after this swap):
  Chat — workflow tasks  : gemma3:12b  (heavier, richer output, ~8 GB RAM)
  Chat — intent / agent  : gemma3:4b   (fast, ~3 GB RAM, 10-20 tok/s on CPU)
  Embeddings             : nomic-embed-text  (768-dim, drop-in for text-embedding-3-small)

The OLLAMA_HOST env var is already set to http://ollama:11434 in
docker-compose.yml for the backend service — no .env change needed for that.
"""

import json
import logging
import os
import time

from dotenv import load_dotenv
from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    OpenAIError,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ── Ollama endpoint ───────────────────────────────────────────────────────────
# Inside Docker Compose the backend reaches Ollama via the service name set in
# docker-compose.yml:  OLLAMA_HOST: http://ollama:11434
# Outside Docker (local dev) falls back to localhost.
_OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_OLLAMA_BASE_URL = _OLLAMA_HOST.rstrip("/") + "/v1"

# ── Model names ───────────────────────────────────────────────────────────────
# Env-var names kept as OPENAI_* so .env only needs value changes, not key
# renames.  Every caller that does
#   from backend.models.openai_client import WORKFLOW_MODEL
# automatically receives the Gemma model name.
WORKFLOW_MODEL = os.getenv("OPENAI_WORKFLOW_MODEL", "gemma3:12b")
INTENT_MODEL   = os.getenv("OPENAI_INTENT_MODEL",   "gemma3:4b")
AGENT_MODEL    = os.getenv("OPENAI_AGENT_MODEL",    "gemma3:4b")
EMBED_MODEL    = os.getenv("OPENAI_EMBED_MODEL",    "nomic-embed-text")

# ── Retry / timeout config ────────────────────────────────────────────────────
MAX_RETRIES      = 3
RETRY_BASE_WAIT  = 1.5   # seconds; doubles each attempt
# Ollama on CPU is slow — give generous room for large prompts
_REQUEST_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))

# Small fast model used as an automatic fallback when the preferred model is
# not yet pulled (e.g. gemma3:12b still downloading at startup)
_SMALL_FALLBACK  = os.getenv("OLLAMA_SMALL_FALLBACK", "gemma3:4b")


def _get_client() -> OpenAI:
    """
    Return an OpenAI SDK client pointed at Ollama's /v1 endpoint.

    Ollama does not require a real API key but the SDK requires a non-empty
    string.  We reuse OPENAI_API_KEY if set (useful for a proxied deployment),
    otherwise use the static placeholder "ollama".
    """
    api_key = os.getenv("OPENAI_API_KEY") or "ollama"
    return OpenAI(
        base_url=_OLLAMA_BASE_URL,
        api_key=api_key,
        timeout=_REQUEST_TIMEOUT,
        max_retries=0,   # we handle retries ourselves below
    )


# ── Model availability helpers ────────────────────────────────────────────────

def _available_models() -> list[str]:
    """Return the list of model IDs currently pulled in Ollama."""
    result = health_check()
    if result.get("status") == "ok":
        return result.get("models", [])
    return []


def _resolve_model(preferred: str) -> str:
    """
    Return `preferred` if Ollama has it; otherwise return the best available
    fallback so calls do not hard-fail while models are still downloading.
    """
    available = _available_models()
    if not available:
        return preferred   # cannot determine — just try
    base = preferred.split(":")[0]
    for m in available:
        if m.startswith(base):
            return m
    # Try the small/fast fallback
    fb_base = _SMALL_FALLBACK.split(":")[0]
    for m in available:
        if m.startswith(fb_base):
            logger.warning(
                "[ollama] '%s' not available, falling back to '%s'", preferred, m
            )
            return m
    logger.warning(
        "[ollama] '%s' not available and no fallback found — proceeding anyway", preferred
    )
    return preferred


# ── Health check ──────────────────────────────────────────────────────────────

def health_check() -> dict:
    """
    Return {"status": "ok", "models": [...]} or {"status": "error", "error": "..."}.

    Shape is identical to the original OpenAI version so main.py line 474
    ("openai": openai_status) keeps working unchanged.
    """
    try:
        client = _get_client()
        models = sorted(m.id for m in client.models.list().data)
        return {"status": "ok", "models": models}
    except OpenAIError as exc:
        logger.warning("[ollama] health_check failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        logger.error("[ollama] health_check unexpected error: %s", exc)
        return {"status": "error", "error": str(exc)}


# ── Text generation ───────────────────────────────────────────────────────────

def chat_with_history(
    model: str,
    system_prompt: str,
    history: list[dict],
    user_message: str,
    temperature: float = 0.7,
) -> str:
    """
    Send system + trimmed history + new user message to the model.

    history is a list of {"role": "user"|"assistant", "content": "..."} dicts.
    Only the last 20 pairs (40 messages) are sent to avoid token overflow.
    Retries on transient errors with exponential back-off.
    """
    MAX_HISTORY_TURNS = 20

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    for msg in trimmed:
        role = msg.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": msg.get("content", "")})
    messages.append({"role": "user", "content": user_message})

    resolved   = _resolve_model(model)
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client   = _get_client()
            response = client.chat.completions.create(
                model=resolved,
                messages=messages,
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()

        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning(
                "[ollama] chat_with_history attempt %d/%d: %s. Retry in %.1fs",
                attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

        except RateLimitError as exc:
            # Ollama does not rate-limit, but a proxy in front might
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            logger.warning(
                "[ollama] rate-limited attempt %d/%d. Sleeping %.1fs",
                attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except OpenAIError as exc:
            logger.error("[ollama] chat_with_history non-retryable: %s", exc)
            raise

    raise RuntimeError(
        f"chat_with_history() failed after {MAX_RETRIES} attempts. Last: {last_exc}"
    )


def generate(
    model: str,
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.7,
) -> str:
    """
    Single-turn plain-text generation.  Retries on transient errors.
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    resolved   = _resolve_model(model)
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client   = _get_client()
            response = client.chat.completions.create(
                model=resolved,
                messages=messages,
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()

        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning(
                "[ollama] generate attempt %d/%d failed (%s). Retrying in %.1fs",
                attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

        except RateLimitError as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            logger.warning(
                "[ollama] rate-limited on attempt %d/%d. Sleeping %.1fs",
                attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except OpenAIError as exc:
            logger.error("[ollama] generate non-retryable error: %s", exc)
            raise

    raise RuntimeError(
        f"generate() failed after {MAX_RETRIES} attempts. Last error: {last_exc}"
    )


def generate_json(
    model: str,
    prompt: str,
    system_prompt: str = "",
    retries: int = 3,
) -> dict:
    """
    Ask the model to return a JSON object.

    Gemma 3 honours the json_object response_format via Ollama's /v1 layer.
    Manual fence-stripping and {}-extraction are kept as defensive fallbacks
    for quantised models that occasionally ignore the format hint.

    If the model rejects response_format entirely (older Ollama builds),
    the call is retried once without it — raw text is then parsed manually.

    Raises ValueError after `retries` failed parse attempts.
    """
    strict_system = (
        (system_prompt + "\n\n" if system_prompt else "")
        + "IMPORTANT: Respond with valid JSON only. "
          "No markdown, no code fences, no prose. "
          "Start your response with { and end with }."
    )

    resolved   = _resolve_model(model)
    last_exc: Exception | None = None

    def _parse_raw(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        start = raw.find("{")
        end   = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]
        return json.loads(raw)

    for attempt in range(1, retries + 1):
        try:
            client   = _get_client()
            messages: list[dict[str, str]] = [
                {"role": "system", "content": strict_system},
                {"role": "user",   "content": prompt},
            ]
            response = client.chat.completions.create(
                model=resolved,
                messages=messages,
                temperature=0.1,
                # Ollama supports json_object for Gemma 3 — same parameter
                # name as the OpenAI API so the call is unchanged.
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()
            return _parse_raw(raw)

        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(
                "[ollama] generate_json parse error attempt %d/%d: %s",
                attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(RETRY_BASE_WAIT)

        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning(
                "[ollama] generate_json transient error attempt %d/%d: %s. Retrying in %.1fs",
                attempt, retries, exc, wait,
            )
            if attempt < retries:
                time.sleep(wait)

        except OpenAIError as exc:
            # Older Ollama builds may reject response_format — retry without it
            if "response_format" in str(exc).lower():
                logger.warning(
                    "[ollama] generate_json: model rejected response_format, retrying without it"
                )
                try:
                    client2   = _get_client()
                    response2 = client2.chat.completions.create(
                        model=resolved,
                        messages=[
                            {"role": "system", "content": strict_system},
                            {"role": "user",   "content": prompt},
                        ],
                        temperature=0.1,
                    )
                    raw2 = (response2.choices[0].message.content or "").strip()
                    return _parse_raw(raw2)
                except (json.JSONDecodeError, Exception) as inner:
                    last_exc = inner
                    if attempt < retries:
                        time.sleep(RETRY_BASE_WAIT)
                    continue
            logger.error("[ollama] generate_json non-retryable: %s", exc)
            raise

    raise ValueError(
        f"generate_json() failed to return valid JSON after {retries} attempts. "
        f"Last error: {last_exc}"
    )


# ── Embeddings ────────────────────────────────────────────────────────────────

def embed(text: str, model: str | None = None) -> list[float]:
    """
    Return an embedding vector for `text` using nomic-embed-text by default.

    IMPORTANT — dimension change vs. OpenAI:
      nomic-embed-text  => 768 dimensions
      text-embedding-3-small => 1536 dimensions

    If your vector store (e.g. pgvector column, Chroma collection) was created
    with OpenAI embeddings you MUST re-embed the entire corpus after switching.
    The new collection/column must be sized for 768 dimensions.

    The `model` override param is kept for API compatibility even though no
    caller in this codebase passes it.
    """
    embed_model = model or EMBED_MODEL
    try:
        client   = _get_client()
        response = client.embeddings.create(
            model=embed_model,
            input=text.replace("\n", " "),
        )
        return response.data[0].embedding
    except OpenAIError as exc:
        logger.error("[ollama] embed failed: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ollama] embed unexpected error: %s", exc)
        raise


# ── Model management ──────────────────────────────────────────────────────────

def pull_model(model_name: str) -> None:
    """
    Pull a model from Ollama's registry via the native /api/pull endpoint.

    This is a real implementation (the original OpenAI version was a no-op)
    because local models must be downloaded before first use.  Streams
    progress lines to stdout so operators can see download progress.
    """
    import httpx

    logger.info(
        "[ollama] Pulling '%s' — may take several minutes on first run", model_name
    )
    print(f"[ollama] Pulling {model_name} ...")
    try:
        with httpx.stream(
            "POST",
            f"{_OLLAMA_HOST}/api/pull",
            json={"name": model_name},
            timeout=600.0,
        ) as r:
            for line in r.iter_lines():
                if line:
                    try:
                        data   = json.loads(line)
                        status = data.get("status", "")
                        if status:
                            print(f"  [{model_name}] {status}")
                    except Exception:
                        pass
    except Exception as exc:
        logger.error("[ollama] pull_model('%s') failed: %s", model_name, exc)
        raise


def ensure_models() -> None:
    """
    Pull any required models that are not yet available in Ollama.

    Called at startup (or manually via CLI) to guarantee all models are
    ready before the first real request arrives.  Safe to call repeatedly —
    already-pulled models are skipped.
    """
    import httpx

    needed = list(dict.fromkeys([WORKFLOW_MODEL, INTENT_MODEL, AGENT_MODEL, EMBED_MODEL]))

    try:
        r = httpx.get(f"{_OLLAMA_HOST}/api/tags", timeout=10.0)
        r.raise_for_status()
        loaded_names: list[str] = [m["name"] for m in r.json().get("models", [])]
    except Exception as exc:
        logger.warning(
            "[ollama] ensure_models: could not reach Ollama (%s) — skipping auto-pull", exc
        )
        return

    for model in needed:
        base    = model.split(":")[0]
        already = any(name.startswith(base) for name in loaded_names)
        if already:
            logger.info("[ollama] ensure_models: '%s' already available — skipping", model)
        else:
            pull_model(model)
