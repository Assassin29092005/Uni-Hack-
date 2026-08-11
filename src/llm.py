"""Provider-agnostic structured LLM call.

One function, `structured_call`, takes a system prompt, a user prompt, and a
pydantic class, and returns a validated instance of that class. Which model
actually answers is decided by the `LLM_PROVIDER` env var.

Why an abstraction here when the project otherwise avoids them: there are three
real implementations, not one. Free-tier cloud (Gemini), fully local with no
account at all (Ollama), and paid (Anthropic). At a hackathon, having a second
path when the first one rate-limits you mid-demo is not speculative engineering.

    LLM_PROVIDER=gemini     (default)  needs GEMINI_API_KEY  — free tier, no card
    LLM_PROVIDER=ollama                needs `ollama serve`  — free, offline, no key
    LLM_PROVIDER=anthropic             needs ANTHROPIC_API_KEY — paid, best quality

All three support schema-constrained JSON output, which is what keeps the rest
of the pipeline identical across them: we never regex a prose reply.
"""

import os
import re
import time
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env into the environment.

    Saves re-exporting a key in every new terminal, and keeps it off the command
    line where it would land in shell history. Real environment variables always
    win, so CI and one-off overrides still work. `.env` is gitignored — check
    that it stays that way before putting a key in it.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()

# Per-provider defaults, each overridable by env var.
MODELS = {
    # Free-tier quota is per model (measured: 20 requests/day/model), so when one
    # is spent, `GEMINI_MODEL=gemini-2.5-flash-lite` gets a fresh bucket.
    "gemini": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    "ollama": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-opus-5"),
}

# Free tiers are rate-limited per MINUTE, not per day, and that is what actually
# bites: 2 calls per record means a handful of parallel workers trips the limit
# immediately. These are conservative starting points — raise them if you have
# a paid key.
DEFAULT_WORKERS = {"gemini": 2, "ollama": 2, "anthropic": 6}

MAX_RETRIES = 4
BACKOFF_BASE = 8   # seconds; 8, 16, 32 — only used when the server names no delay
MAX_BACKOFF = 90   # don't sleep longer than this on one attempt

# Providers put the wait they actually want in the error text. Honour it: a
# blind 4/8/16 backoff totals 28s, so against a quota window that reopens at 42s
# every retry was guaranteed to fail and the record died on a recoverable error.
# Covers the spellings seen in the wild: "Please retry in 41.8s",
# "retryDelay: '17s'", "Retry-After: 30".
_RETRY_AFTER = re.compile(
    r"retry[\s_\-]*(?:after|in|delay)[\s:=\"']*(\d+(?:\.\d+)?)\s*s?",
    re.IGNORECASE,
)


def retry_delay_from(exc: Exception, fallback: float) -> float:
    """Seconds to wait, preferring the provider's own stated delay."""
    match = _RETRY_AFTER.search(str(exc))
    if match:
        # +1s of slack: sleeping until the exact boundary tends to land early.
        return min(float(match.group(1)) + 1, MAX_BACKOFF)
    return min(fallback, MAX_BACKOFF)


class ProviderError(RuntimeError):
    """Setup problem the user has to fix (missing key, server not running)."""


def _is_rate_limit(exc: Exception) -> bool:
    """Providers signal quota exhaustion in incompatible ways, and none of them
    share an exception type. Matching on the message is crude but it is the only
    thing that works across all three without importing each SDK's error tree."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in ("429", "rate limit", "rate_limit",
                                   "resource_exhausted", "quota", "overloaded", "529"))


def _is_permanent(exc: Exception) -> bool:
    """A malformed request or a bad key will fail identically forever.

    Retrying those wastes the user's quota and — worse — buries the real error
    under a wall of retry noise. Learned the hard way: a schema the API rejected
    got retried 4x across 5 records before anyone could read the message.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    if _is_rate_limit(exc):
        return False  # 429 looks like a 4xx but is the retryable one
    return any(s in text for s in ("400", "invalid_argument", "invalid argument",
                                   "401", "403", "404", "permission_denied",
                                   "unauthenticated", "not found", "invalid api key"))


def _close_objects(schema: dict) -> dict:
    """Add `additionalProperties: false` to every object in a JSON schema.

    Anthropic's structured outputs require it; Gemini returns a 400 if it is
    present. So it lives here, applied per provider, rather than on the shared
    pydantic models — one schema, two dialects.
    """
    if isinstance(schema, dict):
        out = {k: _close_objects(v) for k, v in schema.items()}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_close_objects(v) for v in schema]
    return schema


# --- provider implementations -------------------------------------------------
# Each returns raw JSON text. Validation is shared, below, so a provider can
# never skip it.

@lru_cache(maxsize=1)
def _gemini_client():
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ProviderError(
            "GEMINI_API_KEY is not set.\n"
            "  Get a free key (no card required) at https://aistudio.google.com/apikey\n"
            "  PowerShell:  $env:GEMINI_API_KEY = 'AIza...'\n"
            "  bash:        export GEMINI_API_KEY=AIza..."
        )
    from google import genai
    return genai.Client(api_key=key)


def _call_gemini(system: str, user: str, schema: type[BaseModel]) -> str:
    from google.genai import types

    response = _gemini_client().models.generate_content(
        model=MODELS["gemini"],
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,  # the SDK converts the pydantic class itself
            max_output_tokens=8192,
        ),
    )
    if not response.text:
        # Usually a safety block or an empty candidate — surface it rather than
        # letting a None reach json parsing three frames later.
        raise RuntimeError(f"Gemini returned no text (finish reason: "
                           f"{getattr(response.candidates[0], 'finish_reason', '?') if response.candidates else 'no candidates'})")
    return response.text


def _call_ollama(system: str, user: str, schema: type[BaseModel]) -> str:
    import ollama

    try:
        response = ollama.chat(
            model=MODELS["ollama"],
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            format=schema.model_json_schema(),  # Ollama constrains decoding to the schema
            options={"temperature": 0},
        )
    except ConnectionError as exc:
        raise ProviderError(
            f"Cannot reach Ollama: {exc}\n"
            f"  Start it:  ollama serve\n"
            f"  Then:      ollama pull {MODELS['ollama']}"
        ) from exc
    return response.message.content or ""


@lru_cache(maxsize=1)
def _anthropic_client():
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise ProviderError(
            "ANTHROPIC_API_KEY is not set (this provider is paid — "
            "use LLM_PROVIDER=gemini for the free tier)."
        )
    import anthropic
    return anthropic.Anthropic()


def _call_anthropic(system: str, user: str, schema: type[BaseModel]) -> str:
    response = _anthropic_client().messages.create(
        model=MODELS["anthropic"],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema",
                                  "schema": _close_objects(schema.model_json_schema())}},
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("Anthropic safety classifiers declined this request")
    return next((b.text for b in response.content if b.type == "text"), "")


_PROVIDERS = {"gemini": _call_gemini, "ollama": _call_ollama, "anthropic": _call_anthropic}


# --- public surface -----------------------------------------------------------

def workers_default() -> int:
    return DEFAULT_WORKERS.get(PROVIDER, 2)


def check_ready() -> None:
    """Fail fast, before a batch spawns threads and burns half a quota finding
    out that the key is missing."""
    if PROVIDER not in _PROVIDERS:
        raise ProviderError(
            f"Unknown LLM_PROVIDER={PROVIDER!r}. Use one of: {', '.join(_PROVIDERS)}"
        )
    if PROVIDER == "gemini":
        _gemini_client()
    elif PROVIDER == "anthropic":
        _anthropic_client()
    elif PROVIDER == "ollama":
        import ollama
        try:
            installed = {m.model for m in ollama.list().models}
        except Exception as exc:  # noqa: BLE001 — any failure here means "not usable"
            raise ProviderError(
                f"Cannot reach Ollama ({type(exc).__name__}).\n"
                f"  Start it:  ollama serve\n"
                f"  Then:      ollama pull {MODELS['ollama']}"
            ) from exc
        if MODELS["ollama"] not in installed:
            raise ProviderError(
                f"Ollama is running but {MODELS['ollama']!r} is not pulled.\n"
                f"  Pull it:   ollama pull {MODELS['ollama']}\n"
                f"  Installed: {', '.join(sorted(installed)) or '(none)'}"
            )


def structured_call(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    """Ask the configured provider for one schema-shaped answer.

    Retries on rate limits with exponential backoff, because every free tier
    limits per minute and this pipeline makes two calls per record. Without
    this, a batch of ten products dies partway through on the free tier.

    Malformed JSON is retried too: schema-constrained decoding makes it rare,
    but "rare" across a few hundred records is "happens".
    """
    if PROVIDER not in _PROVIDERS:
        raise ProviderError(
            f"Unknown LLM_PROVIDER={PROVIDER!r}. Use one of: {', '.join(_PROVIDERS)}"
        )
    call = _PROVIDERS[PROVIDER]
    last: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            return schema.model_validate_json(call(system, user, schema))
        except ProviderError:
            raise  # setup problem — retrying will not fix a missing API key
        except Exception as exc:  # noqa: BLE001 — retry policy is deliberately broad
            last = exc
            if _is_permanent(exc):
                raise RuntimeError(f"{PROVIDER} rejected the request: {exc}") from exc
            if attempt == MAX_RETRIES - 1:
                break
            delay = retry_delay_from(exc, BACKOFF_BASE * (2 ** attempt))
            reason = "rate limited" if _is_rate_limit(exc) else type(exc).__name__
            print(f"    [{reason}; retry {attempt + 1}/{MAX_RETRIES - 1} in {delay:.0f}s]")
            time.sleep(delay)

    raise RuntimeError(f"{PROVIDER} failed after {MAX_RETRIES} attempts: {last}") from last


def describe() -> str:
    """One line for logs, so a run's output records which model produced it."""
    return f"{PROVIDER} / {MODELS.get(PROVIDER, '?')}"

# ponytail: rate-limit detection matches on the exception message because the
# three SDKs share no error hierarchy. It over-matches at worst (an extra retry
# on an unrelated error), which is the harmless direction. Upgrade path if it
# gets noisy: import each SDK's error type inside its own branch and catch
# properly there.
