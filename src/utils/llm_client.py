"""Provider-agnostic LLM chat wrapper.

The bot has four LLM-using modules (advisor, news sentiment, market regime,
EOD reflection). They all want the same thing: hand the model a prompt (plus
optional system prompt) and get a string back. This module gives them one
function — ``chat()`` — that hides *which* provider they're talking to.

Supported providers (selected via ``llm.provider`` in settings.yaml):

  - ``anthropic``    -> Anthropic Messages API (cloud, needs ANTHROPIC_API_KEY)
  - ``lmstudio``     -> LM Studio's OpenAI-compatible server (local, no key)
  - ``openai_compat``/``local`` -> any OpenAI-chat-compatible endpoint
                        (Ollama's /v1, vLLM, LocalAI, llama.cpp server, etc.)

For local providers, ``llm.base_url`` is the endpoint root
(default: ``http://localhost:1234/v1`` which is LM Studio's default) and
``llm.model`` is whatever model identifier the server expects. LM Studio
accepts either the loaded model's id (shown in the top bar of LM Studio) or
any string — it uses the currently-loaded model regardless.

If you want to use a different local runtime:
  - Ollama with OpenAI compat: base_url = http://localhost:11434/v1
  - LocalAI:                   base_url = http://localhost:8080/v1
  - vLLM (default):            base_url = http://localhost:8000/v1
"""
from __future__ import annotations

from typing import Optional

from .config import load_config
from .logger import get_logger

log = get_logger(__name__)

# Flipped to False the first time we get a 400 on response_format so we
# never send it again this session — avoids repeating the warning every call.
_json_mode_supported: bool = True


def chat(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 400,
    temperature: float = 0.2,
    json_mode: bool = False,
    tag: str = "unknown",
) -> str:
    """Send a single-turn chat request and return the assistant text.

    Raises on network / auth / shape errors — the caller decides whether to
    swallow and fall back (e.g. news_sentiment drops back to the lexicon
    scorer; reflection drops back to a non-LLM summary).

    ``json_mode``: if True and the provider supports it, constrain the model
    to emit a valid JSON object. Supported by LM Studio / any OpenAI-compat
    server via ``response_format={"type":"json_object"}``. A no-op for the
    Anthropic provider and for servers that ignore the field (the prompt
    should still ask for JSON either way — JSON mode is belt-and-suspenders).
    """
    cfg = load_config()
    provider = _normalize_provider(cfg.get("llm", {}).get("provider", "anthropic"))

    if provider == "anthropic":
        raw = _chat_anthropic(prompt, system, max_tokens, temperature, cfg)
    elif provider in ("lmstudio", "openai_compat", "local"):
        raw = _chat_openai_compat(prompt, system, max_tokens, temperature, cfg, json_mode)
    else:
        raise RuntimeError(f"unknown llm.provider: {provider!r}")

    _log_llm_call(tag, prompt, raw, max_tokens, cfg)
    return raw


def _log_llm_call(tag: str, prompt: str, response: str, max_tokens: int, cfg: dict) -> None:
    """Append a row to data/llm_logs/YYYY-MM-DD.jsonl for every LLM call.
    Lets you audit raw outputs for hallucinations outside of bot cycles.
    """
    import json as _json
    from datetime import datetime, timezone
    from .config import project_root

    try:
        now = datetime.now(timezone.utc)
        log_dir = project_root() / "data" / "llm_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
        row = {
            "ts":            now.isoformat(),
            "tag":           tag,
            "model":         (cfg.get("llm") or {}).get("model", "unknown"),
            "max_tokens":    max_tokens,
            "prompt_chars":  len(prompt),
            "response_chars": len(response),
            "response":      response,
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let logging break a live call


def llm_available() -> tuple[bool, str]:
    """Cheap gate: can the configured provider be called at all?

    Returns (ok, reason). Used by call sites to decide whether to skip the
    LLM entirely (e.g. when no key is set and the provider is cloud).

    Note: for local providers this does NOT actually ping the server — it
    just checks configuration. A local server that's configured but not
    running will surface as a connection error from ``chat()`` itself,
    which call sites already handle with a fallback.
    """
    cfg = load_config()
    provider = _normalize_provider(cfg.get("llm", {}).get("provider", "anthropic"))
    if provider == "anthropic":
        if not cfg["secrets"].get("anthropic_api_key"):
            return False, "no ANTHROPIC_API_KEY"
        return True, "anthropic"
    if provider in ("lmstudio", "openai_compat", "local"):
        base = cfg.get("llm", {}).get("base_url", "http://localhost:1234/v1")
        return True, f"{provider} @ {base}"
    return False, f"unknown provider: {provider}"


def provider_label() -> str:
    """Short string like 'anthropic' or 'lmstudio' — handy for log lines."""
    cfg = load_config()
    return _normalize_provider(cfg.get("llm", {}).get("provider", "anthropic"))


def llm_ping(timeout: float = 5.0) -> dict:
    """Live reachability check for the configured provider.

    For local providers this actually hits ``GET {base_url}/models`` — the
    standard OpenAI-compatible health endpoint. It's cheap (no inference,
    usually <50ms) and also tells us which model is currently loaded so the
    dashboard can show it.

    For Anthropic there's no equivalent cheap health endpoint, so we just
    confirm the API key is set. (A real network check would cost a full
    message round-trip, which is too expensive to do on every dashboard
    refresh.)

    Returns a dict with:
      - ok: bool
      - provider: str
      - model: str | None   (loaded model for local, configured model for cloud)
      - base_url: str | None
      - latency_ms: float | None
      - error: str | None
    """
    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    provider = _normalize_provider(llm_cfg.get("provider", "anthropic"))
    configured_model = llm_cfg.get("model") or None

    if provider == "anthropic":
        has_key = bool(cfg["secrets"].get("anthropic_api_key"))
        return {
            "ok": has_key,
            "provider": "anthropic",
            "model": configured_model,
            "base_url": None,
            "latency_ms": None,
            "error": None if has_key else "no ANTHROPIC_API_KEY set",
        }

    if provider in ("lmstudio", "openai_compat", "local"):
        import time
        import requests

        base_url = str(llm_cfg.get("base_url") or "http://localhost:1234/v1").rstrip("/")
        url = f"{base_url}/models"
        headers = {}
        local_key = (cfg["secrets"].get("local_llm_api_key") or "").strip()
        if local_key:
            headers["Authorization"] = f"Bearer {local_key}"

        t0 = time.time()
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            latency_ms = round((time.time() - t0) * 1000, 1)
        except requests.exceptions.ConnectionError:
            return {
                "ok": False, "provider": provider, "model": None,
                "base_url": base_url, "latency_ms": None,
                "error": f"cannot reach {base_url} — is the local server running?",
            }
        except Exception as e:  # pragma: no cover — unlikely path
            return {
                "ok": False, "provider": provider, "model": None,
                "base_url": base_url, "latency_ms": None,
                "error": str(e)[:200],
            }

        if not r.ok:
            return {
                "ok": False, "provider": provider, "model": None,
                "base_url": base_url, "latency_ms": latency_ms,
                "error": f"HTTP {r.status_code} from {url}",
            }

        try:
            body = r.json()
            models = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
        except Exception:
            models = []
        loaded_model = models[0] if models else configured_model
        return {
            "ok": True,
            "provider": provider,
            "model": loaded_model,
            "base_url": base_url,
            "latency_ms": latency_ms,
            "error": None,
        }

    return {
        "ok": False, "provider": provider, "model": None, "base_url": None,
        "latency_ms": None, "error": f"unknown provider: {provider}",
    }


# ------------------------------------------------------------------- internals

def _normalize_provider(raw) -> str:
    p = str(raw or "anthropic").lower().replace("-", "_")
    if p in ("openai_compatible",):
        return "openai_compat"
    return p


def _chat_anthropic(prompt, system, max_tokens, temperature, cfg) -> str:
    from anthropic import Anthropic  # imported lazily so local-only setups don't need it

    api_key = cfg["secrets"].get("anthropic_api_key", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=api_key)
    kwargs = {
        "model": cfg["llm"]["model"],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text.strip()


def _chat_openai_compat(prompt, system, max_tokens, temperature, cfg, json_mode: bool = False) -> str:
    """POST to an OpenAI-style /chat/completions endpoint.

    LM Studio accepts any model string (it uses whatever is loaded in the UI),
    and doesn't require auth by default. If the user set a key — some setups
    do — pass it as a Bearer token.

    If ``json_mode`` is True, asks the server to constrain output to valid
    JSON via ``response_format={"type":"json_object"}`` — LM Studio and most
    OpenAI-compat servers honour this. Servers that don't recognize the field
    just ignore it, which is why the prompt still has to ask for JSON.
    """
    import requests

    llm_cfg = cfg.get("llm", {}) or {}
    base_url = str(llm_cfg.get("base_url") or "http://localhost:1234/v1").rstrip("/")
    url = f"{base_url}/chat/completions"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": llm_cfg.get("model") or "local-model",
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }
    global _json_mode_supported
    if json_mode and _json_mode_supported:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    local_key = (cfg["secrets"].get("local_llm_api_key") or "").strip()
    if local_key:
        headers["Authorization"] = f"Bearer {local_key}"

    timeout = int(llm_cfg.get("timeout_seconds", 120))
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"could not reach local LLM at {url} — is LM Studio running with "
            f"Local Server enabled? ({e})"
        ) from e
    # Older LM Studio builds don't recognize response_format and 400 the whole
    # request. Retry once without it, then remember not to send it again this
    # session so the warning only appears once.
    if r.status_code == 400 and json_mode and "response_format" in payload:
        log.warning("LLM server rejected response_format — retrying without json_mode (won't retry again this session)")
        _json_mode_supported = False
        payload.pop("response_format", None)
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"could not reach local LLM at {url} — is LM Studio running with "
                f"Local Server enabled? ({e})"
            ) from e
    r.raise_for_status()
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected response shape from {url}: {str(data)[:200]}") from e
    return _strip_reasoning_tags(text).strip()


def extract_json_object(text: str) -> dict:
    """Pull the first complete JSON object out of LLM response text.

    Handles the common mess that reasoning / chatty models produce:
      - leading prose ("Sure! Here is your JSON:")
      - markdown code fences (```json ... ```)
      - trailing prose or second JSON objects after the real one
      - the "Extra data: line N column M" case that plain json.loads chokes on

    Raises ValueError with the first 200 chars of the response if no parseable
    JSON object can be found. Callers should catch and fall back.
    """
    import json as _json
    import re as _re
    if not text:
        raise ValueError("empty response")
    # Strip ``` code fences if present — they're the most common wrapper
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening ```lang line and the trailing ``` if present
        cleaned = _re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", cleaned)
        cleaned = _re.sub(r"\n?```\s*$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError(f"no JSON in response: {text[:200]}")
    cleaned = cleaned[start:]

    # First attempt: raw_decode as-is. Handles the common chatty-trailing-prose case.
    decoder = _json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(cleaned)
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON root is {type(obj).__name__}, expected object")
    except _json.JSONDecodeError:
        pass  # fall through to the lenient pass

    # Second attempt: clean up JS-isms reasoning models often emit inside JSON:
    #   - // line comments ("confidence": 0.3, // my thinking goes here")
    #   - /* block comments */
    #   - trailing commas before } or ]
    # We do this carefully — string contents that happen to contain // or ,}
    # are preserved by walking char-by-char and tracking whether we're inside
    # a string literal.
    lenient = _strip_json_comments_and_trailing_commas(cleaned)
    try:
        obj, _end = decoder.raw_decode(lenient)
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON root is {type(obj).__name__}, expected object")
    except _json.JSONDecodeError:
        pass  # fall through to ASCII-safe pass

    # Third attempt: replace non-ASCII and bare control chars with spaces so that
    # LLM responses containing foreign-language text (e.g. Chinese reasoning traces
    # or embedded translations) don't permanently crash the JSON decoder.
    ascii_safe = "".join(
        c if (32 <= ord(c) < 128 or c in "\t\n\r") else " " for c in lenient
    )
    try:
        obj, _end = decoder.raw_decode(ascii_safe)
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON root is {type(obj).__name__}, expected object")
    except _json.JSONDecodeError as e:
        raise ValueError(f"JSON parse failed: {e}. Raw: {text[:200]}") from e


def _strip_json_comments_and_trailing_commas(s: str) -> str:
    """Remove // and /* */ comments and trailing commas from a JSON-ish string,
    while preserving the contents of string literals verbatim.

    Reasoning models sometimes emit invalid JSON with JavaScript-style comments
    ("confidence": 0.35,  // because technicals are...) — this makes those
    parseable without breaking strings that legitimately contain // or commas.
    """
    out = []
    i = 0
    n = len(s)
    in_string = False
    escape = False
    while i < n:
        c = s[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        # Line comment //...
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        # Block comment /* ... */
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2  # skip the closing */
            continue
        out.append(c)
        i += 1
    result = "".join(out)
    # Strip trailing commas before } or ]
    import re as _re
    result = _re.sub(r",(\s*[}\]])", r"\1", result)
    return result


def _strip_reasoning_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks emitted by reasoning models
    (DeepSeek R1, QwQ, etc.). The bot's JSON-parsing call sites (regime, news,
    advisor) would otherwise choke on the prefix and silently fall back to their
    non-LLM paths.

    Also strips the couple of other common wrappers we've seen in the wild.
    Non-reasoning models never emit these tags so this is a no-op for them.
    """
    import re
    if not text:
        return text
    for tag in ("think", "reasoning", "thought"):
        # Closed block — non-greedy, DOTALL so it spans newlines
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Unterminated: some models run out of tokens mid-think and never close
        # the tag — strip from the opening tag to end of string
        text = re.sub(rf"<{tag}>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text
