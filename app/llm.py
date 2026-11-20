"""Claude wrapper. Every caller has a non-LLM fallback, so the pipeline still runs without a key."""
import json
import logging

import anthropic

import time

from . import config, db

log = logging.getLogger("signalpipe.llm")
_client: anthropic.AsyncAnthropic | None = None
last_error: str = ""

# Models that support server-side refusal fallbacks.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")


# List prices per million tokens (input, output, cache read). Used only for the spend estimate in Settings.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-haiku-5-5": (0.10, 0.50, 0.01)}


def record_usage(model: str, usage) -> None:
    """Add one response's tokens and web searches to this month's running total."""
    if usage is None:
        return
    month = time.strftime("%Y-%m")
    total = db.get_setting("usage", {}) or {}
    m = total.setdefault(month, {})
    for k in ("input", "output", "cache_read", "searches", "calls"):
        m.setdefault(k, 0)
    m.setdefault("usd", 0.0)
    inp = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    pi, po, pc = PRICES.get(model, PRICES["claude-opus-5-5"])
    m["input"] += inp
    m["output"] += out
    m["cache_read"] += cached
    m["searches"] += searches
    m["calls"] += 1
    m["usd"] = round(m["usd"] + (inp * pi + out * po + cached * pc) / 1e6 + searches * WEB_SEARCH_USD, 4)
    db.set_setting("usage", {k: v for k, v in sorted(total.items())[-6:]})


WEB_SEARCH_USD = 0.01  # estimated cost of one web search, added to the spend estimate


def _count_subscription_call() -> None:
    month = time.strftime("%Y-%m")
    total = db.get_setting("usage", {}) or {}
    m = total.setdefault(month, {"input": 0, "output": 0, "cache_read": 0, "searches": 0, "usd": 0.0, "calls": 0})
    m["subscription_calls"] = m.get("subscription_calls", 0) + 1
    db.set_setting("usage", total)


def subscription() -> bool:
    """True when Claude runs through Claude Code on the person's subscription rather than an API key."""
    return config.ENGINE == "claude_code"


def over_budget() -> bool:
    if subscription():  # a plan has usage limits, not a bill; Claude Code reports when one is reached
        return False
    cap = config.AGENT_BUDGET_USD
    return bool(cap and cap > 0 and usage_this_month().get("usd", 0) >= cap)


def budget_message() -> str:
    return (f"This month's Claude budget is used up (about ${usage_this_month().get('usd', 0):.2f} of "
            f"${config.AGENT_BUDGET_USD:.2f}). Raise it in Settings, or the AI features resume next month.")


def usage_this_month() -> dict:
    return (db.get_setting("usage", {}) or {}).get(time.strftime("%Y-%m"), {"input": 0, "output": 0, "searches": 0, "usd": 0.0, "calls": 0})


def client() -> anthropic.AsyncAnthropic:
    return _get()


def fallback_kwargs(model: str) -> dict:
    """Server-side refusal fallback, on the models that support it."""
    return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model.startswith(_FALLBACK_MODELS) else {}


def reset() -> None:
    """Forget the client so the next call picks up a changed API key."""
    global _client, last_error
    _client = None
    last_error = ""


def available() -> bool:
    return config.status()["llm"]


def _get() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=2, timeout=180.0)
    return _client


async def ask_json(system: str, prompt: str, schema: dict, effort: str = "low", max_tokens: int = 8000) -> dict | None:
    """One structured-output call. Returns the parsed object, or None on any failure (callers fall back)."""
    global last_error
    if not available():
        return None
    if subscription():
        from . import claude_code
        data = await claude_code.ask_json(system, prompt, schema)
        last_error = claude_code.last_error if data is None else ""
        if data is not None:
            _count_subscription_call()
        return data
    if over_budget():
        last_error = budget_message()
        return None
    kwargs = dict(
        model=config.LLM_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    try:
        if config.LLM_MODEL.startswith(_FALLBACK_MODELS):
            resp = await _get().beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = await _get().messages.create(**kwargs)
    except anthropic.AuthenticationError:
        last_error = "Anthropic API key was rejected"
        log.error(last_error)
        return None
    except anthropic.RateLimitError:
        last_error = "Anthropic rate limit hit; falling back to heuristics for this call"
        log.warning(last_error)
        return None
    except anthropic.APIStatusError as e:
        last_error = f"Anthropic API error {e.status_code}: {e.message}"
        log.error(last_error)
        return None
    except anthropic.APIConnectionError:
        last_error = "Could not reach the Anthropic API"
        log.error(last_error)
        return None
    record_usage(config.LLM_MODEL, getattr(resp, "usage", None))
    if resp.stop_reason == "refusal":
        last_error = "Model declined the request"
        return None
    if resp.stop_reason == "max_tokens":
        last_error = "Model output was cut off (max_tokens)"
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        last_error = ""
        return json.loads(text)
    except ValueError:
        last_error = "Model returned invalid JSON"
        return None


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}
STRS = {"type": "array", "items": {"type": "string"}}
