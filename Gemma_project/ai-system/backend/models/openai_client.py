"""
OpenAI client — drop-in replacement for ollama_client.py.

All public names (generate, generate_json, embed, health_check, WORKFLOW_MODEL,
INTENT_MODEL, AGENT_MODEL, EMBED_MODEL) are identical to ollama_client so every
import in the codebase can be switched with a single search-replace.
"""

import json
import logging
import os
import time
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError, OpenAIError

load_dotenv()

logger = logging.getLogger(__name__)

# ── Model names from env ──────────────────────────────────────────────────────
WORKFLOW_MODEL = os.getenv("OPENAI_WORKFLOW_MODEL", "gpt-4o")
INTENT_MODEL   = os.getenv("OPENAI_INTENT_MODEL",   "gpt-4o-mini")
AGENT_MODEL    = os.getenv("OPENAI_AGENT_MODEL",    "gpt-4o-mini")
EMBED_MODEL    = os.getenv("OPENAI_EMBED_MODEL",    "text-embedding-3-small")

# ── Retry config ──────────────────────────────────────────────────────────────
MAX_RETRIES     = 3
RETRY_BASE_WAIT = 1.5   # seconds; doubles each attempt


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to your .env file."
        )
    return OpenAI(api_key=api_key)


# ── Health check ──────────────────────────────────────────────────────────────

def health_check() -> dict:
    """Return {"status": "ok", "models": [...]} or {"status": "error", "error": "..."}."""
    try:
        client = _get_client()
        models = [m.id for m in client.models.list().data if "gpt" in m.id]
        return {"status": "ok", "models": sorted(models)}
    except OpenAIError as exc:
        logger.warning("[openai] health_check failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        logger.error("[openai] health_check unexpected error: %s", exc)
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
    Send a full conversation (system + history + new user message) to the model.

    This is the correct way to maintain multi-turn context with OpenAI —
    pass the entire messages array so the model sees the full conversation,
    not just a flattened text string.

    `history` is a list of {"role": "user"|"assistant", "content": "..."} dicts.
    Only the last MAX_HISTORY_TURNS are sent to avoid token overflow.
    """
    MAX_HISTORY_TURNS = 20  # 20 pairs = up to 40 messages

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    # Trim history to last N turns
    trimmed = history[-(MAX_HISTORY_TURNS * 2):]
    for msg in trimmed:
        role = msg.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": msg.get("content", "")})

    messages.append({"role": "user", "content": user_message})

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()

        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning("[openai] chat_with_history attempt %d/%d: %s. Retry in %.1fs", attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)

        except RateLimitError as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            logger.warning("[openai] rate-limited attempt %d/%d. Sleeping %.1fs", attempt, MAX_RETRIES, wait)
            time.sleep(wait)

        except OpenAIError as exc:
            logger.error("[openai] chat_with_history non-retryable: %s", exc)
            raise

    raise RuntimeError(f"chat_with_history() failed after {MAX_RETRIES} attempts. Last: {last_exc}")


def generate(
    model: str,
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.7,
) -> str:
    """
    Send a completion request and return the assistant text.

    Retries on transient errors (connection, timeout, rate-limit) with
    exponential back-off.  Raises on permanent failures.
    """
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            content = response.choices[0].message.content or ""
            return content.strip()

        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning(
                "[openai] generate attempt %d/%d failed (%s). Retrying in %.1fs…",
                attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

        except RateLimitError as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            logger.warning(
                "[openai] rate-limited on attempt %d/%d. Sleeping %.1fs…",
                attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except OpenAIError as exc:
            # Non-retryable API error (auth, invalid model, etc.)
            logger.error("[openai] generate non-retryable error: %s", exc)
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

    Uses OpenAI's json_object response_format when available so we never get
    markdown fences.  Falls back to manual extraction if the model ignores the
    format hint.  Raises ValueError after `retries` failed parse attempts.
    """
    strict_system = (
        (system_prompt + "\n\n" if system_prompt else "")
        + "IMPORTANT: Respond with valid JSON only. "
          "No markdown, no code fences, no prose. "
          "Start your response with { and end with }."
    )

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client = _get_client()
            messages: list[dict[str, str]] = [
                {"role": "system", "content": strict_system},
                {"role": "user",   "content": prompt},
            ]
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()

            # Strip accidental markdown fences
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            start = raw.find("{")
            end   = raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start : end + 1]

            return json.loads(raw)

        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(
                "[openai] generate_json parse error on attempt %d/%d: %s",
                attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(RETRY_BASE_WAIT)

        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            last_exc = exc
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            logger.warning(
                "[openai] generate_json transient error attempt %d/%d: %s. Retrying in %.1fs…",
                attempt, retries, exc, wait,
            )
            if attempt < retries:
                time.sleep(wait)

        except OpenAIError as exc:
            logger.error("[openai] generate_json non-retryable: %s", exc)
            raise

    raise ValueError(
        f"generate_json() failed to return valid JSON after {retries} attempts. "
        f"Last error: {last_exc}"
    )


# ── Embeddings ────────────────────────────────────────────────────────────────

def embed(text: str, model: str | None = None) -> list[float]:
    """
    Return an embedding vector for `text`.

    Raises on failure — callers should wrap with try/except if a fallback is
    acceptable (e.g. RAG can degrade gracefully without embeddings).
    """
    embed_model = model or EMBED_MODEL
    try:
        client = _get_client()
        response = client.embeddings.create(
            model=embed_model,
            input=text.replace("\n", " "),
        )
        return response.data[0].embedding
    except OpenAIError as exc:
        logger.error("[openai] embed failed: %s", exc)
        raise
    except Exception as exc:
        logger.error("[openai] embed unexpected error: %s", exc)
        raise


# ── Compatibility stubs (were in ollama_client, not needed for OpenAI) ────────

def pull_model(model_name: str) -> None:
    """No-op: OpenAI models are always available via API."""
    logger.info("[openai] pull_model('%s') — no-op for OpenAI", model_name)


def ensure_models() -> None:
    """No-op: OpenAI models do not require local pulling."""
    logger.info("[openai] ensure_models() — no-op for OpenAI")
