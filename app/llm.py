"""Claude wrapper. Every caller has a non-LLM fallback, so the pipeline still runs without a key."""
import json
import logging

import anthropic

from . import config

log = logging.getLogger("signalpipe.llm")
_client: anthropic.AsyncAnthropic | None = None
last_error: str = ""

# Models that support server-side refusal fallbacks.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")


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
