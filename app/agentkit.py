"""The engine every agent runs on.

One loop, shared by all agents: call Claude with the agent's tools, run the tools it asks for, send the results back,
and continue until it is done. Along the way it records every step and its cost in `agent_runs`, honours each
agent's on/off switch, and stops when the month's Claude spend reaches the cap set in Settings.

Tools are typed Python functions decorated with anthropic.beta_async_tool (the SDK builds their schemas), plus
Anthropic's server-side web search and web fetch, which run on Anthropic's side and need no code here.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

from . import config, db, llm

log = logging.getLogger("signalpipe.agents")

AGENTS = {
    "prospector": ("Prospector", "Searches the web for companies that fit your customer profile and show a buying trigger "
                   "right now, plus lookalikes of the leads you approve and win. Runs with each play that has it as a source."),
    "people": ("People finder", "Finds the right named buyers at a company, with LinkedIn profiles it saw and work emails "
               "it checked. Runs on the best new leads after each play run, and when you approve a lead."),
    "writer": ("Writer", "Reads the evidence about a prospect, writes the sequence, and checks every message against "
               "your rules before saving it. Runs when you approve a lead."),
    "inbox": ("Inbox", "Handles each reply: drafts your answer, schedules a follow-up when someone says not now, adds "
              "people you were referred to, records meetings and unsubscribes, and asks you what it cannot answer."),
    "reengage": ("Re-engagement", "When a lead that went quiet shows a new signal, drafts a fresh message about it."),
    "strategist": ("Strategist", "Reviews results once a week and proposes changes to your plays, buyer titles and "
                   "messaging, which you apply with one click."),
    "content": ("Content writer", "Turns the briefs on the SEO page into full article drafts, researched on the web."),
    "copilot": ("Copilot", "A chat that runs the rest for you: find companies, research them, create plays, approve leads."),
}
WEB_SEARCH_USD = 0.01  # per search; replaced by the published price in llm.WEB_SEARCH_USD when set


def is_on(name: str) -> bool:
    return bool((db.get_setting("agents_on", {}) or {}).get(name, True))


def set_on(name: str, on: bool) -> None:
    flags = db.get_setting("agents_on", {}) or {}
    flags[name] = bool(on)
    db.set_setting("agents_on", flags)


def web_tools(model: str, searches: int = 5, fetches: int = 4) -> list[dict]:
    """Anthropic's server-side web tools. The dynamic-filtering versions are not offered on Haiku."""
    haiku = model.startswith("claude-haiku")
    tools = []
    if searches:
        tools.append({"type": "web_search_20250305" if haiku else "web_search_20260209", "name": "web_search", "max_uses": searches})
    if fetches:
        tools.append({"type": "web_fetch_20250910" if haiku else "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches})
    return tools


@dataclass
class Run:
    ok: bool
    error: str = ""
    final_text: str = ""
    steps: list = field(default_factory=list)
    usd: float = 0.0
    searches: int = 0
    run_id: int = 0
    messages: list = field(default_factory=list)  # the whole conversation, for agents that keep one
    session_id: str = ""  # with Claude Code: the session to resume for the next turn of a conversation


def _brief(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cost(model: str, usage) -> tuple[float, int]:
    pi, po, pc = llm.PRICES.get(model, llm.PRICES["claude-opus-5-5"])
    inp = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    return (inp * pi + out * po + cached * pc) / 1e6 + searches * llm.WEB_SEARCH_USD, searches


def to_param(content) -> list[dict]:
    """A response's content as plain dicts, exactly as the API needs it back (thinking blocks included)."""
    out = []
    for b in content:
        out.append(b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b)
    return out


async def run(agent: str, *, system: str, messages: list, tools: list, subject: str = "", effort: str = "medium",
              max_turns: int = 14, max_tokens: int = 16000, web: list | None = None, force: bool = False,
              resume: str = "") -> Run:
    """Run one agent to completion. `tools` are beta_async_tool functions; `web` adds server-side web tools."""
    if not force and not is_on(agent):
        return Run(False, f"The {AGENTS[agent][0]} agent is switched off on the Agents page.")
    if not llm.available():
        return Run(False, "Connect Claude in Settings (an API key, or Claude Code on your subscription) to use the agents.")
    if llm.subscription():
        return await _run_claude_code(agent, system=system, messages=messages, tools=tools, subject=subject,
                                      web=bool(web), resume=resume)
    if llm.over_budget():
        return Run(False, llm.budget_message())
    model = config.LLM_MODEL
    by_name = {t.name: t for t in tools}
    schemas = [t.to_dict() for t in tools] + list(web or [])
    run_id = db.x("INSERT INTO agent_runs(agent,subject,started_at,status) VALUES(?,?,?,?)",
                  (agent, subject[:200], db.now(), "running"))
    result = Run(False, run_id=run_id, messages=list(messages))
    msgs = result.messages
    try:
        for _ in range(max_turns):
            if llm.over_budget():
                result.error = llm.budget_message()
                break
            resp = await llm.client().beta.messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=schemas, messages=msgs,
                output_config={"effort": effort}, **llm.fallback_kwargs(model))
            llm.record_usage(model, resp.usage)
            usd, searches = _cost(model, resp.usage)
            result.usd += usd
            result.searches += searches
            for b in resp.content:
                if b.type == "server_tool_use":
                    result.steps.append({"tool": b.name, "input": _brief(b.input)})
            msgs.append({"role": "assistant", "content": to_param(resp.content)})
            if resp.stop_reason == "pause_turn":  # a long server-side turn; sending it back continues it
                continue
            if resp.stop_reason == "refusal":
                result.error = "Claude declined this task."
                break
            if resp.stop_reason == "max_tokens":
                result.error = "The agent's reply was cut off."
                break
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                result.final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                result.ok = True
                break

            async def call(b):
                tool = by_name.get(b.name)
                if tool is None:
                    return b, f"Error: there is no tool called {b.name}.", True
                try:
                    out = await tool.func(**(b.input or {}))
                    return b, out if isinstance(out, str) else json.dumps(out, default=str), False
                except TypeError as e:
                    return b, f"Error: wrong arguments for {b.name}: {e}", True
                except Exception as e:  # report to Claude so it can try another way; log for us
                    log.warning("%s tool %s failed: %s", agent, b.name, e)
                    return b, f"Error: {type(e).__name__}: {str(e)[:300]}", True
            done = await asyncio.gather(*[call(b) for b in calls])
            for b, out, err in done:
                result.steps.append({"tool": b.name, "input": _brief(b.input), "result": _brief(out, 200), "error": err})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": out, **({"is_error": True} if err else {})}
                for b, out, err in done]})
        else:
            result.error = "The agent used all its steps without finishing."
    except anthropic.AuthenticationError:
        result.error = "The Claude API key was rejected. Check it in Settings."
    except anthropic.RateLimitError:
        result.error = "Claude is rate limiting this key right now; try again in a minute."
    except anthropic.APIStatusError as e:
        result.error = f"Claude API error {e.status_code}: {str(e.message)[:200]}"
    except anthropic.APIConnectionError:
        result.error = "Could not reach the Claude API."
    status = "ok" if result.ok else "error"
    db.x("UPDATE agent_runs SET finished_at=?, status=?, summary=?, usd=?, searches=?, steps=? WHERE id=?",
         (db.now(), status, (result.final_text or result.error)[:500], round(result.usd, 4), result.searches,
          db.j(result.steps[-60:]), run_id))
    return result


def _as_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


async def _run_claude_code(agent: str, *, system: str, messages: list, tools: list, subject: str, web: bool,
                           resume: str) -> Run:
    """The same run, through Claude Code on the person's subscription. Steps are logged the same way."""
    from . import claude_code
    run_id = db.x("INSERT INTO agent_runs(agent,subject,started_at,status) VALUES(?,?,?,?)",
                  (agent, subject[:200], db.now(), "running"))
    prompt = _as_text(messages[-1]["content"]) if messages else ""
    if len(messages) > 1 and not resume:  # an earlier conversation without a Claude Code session: replay it as text
        prompt = "\n\n".join(f"{m['role'].upper()}: {_as_text(m['content'])}" for m in messages if _as_text(m["content"]))
    res = await claude_code.run(system=system, prompt=prompt, tools=tools, web=web, resume=resume or None)
    result = Run(bool(res.get("ok")), res.get("error") or "", res.get("text") or "", res.get("steps") or [], 0.0,
                 res.get("searches") or 0, run_id, list(messages), res.get("session_id") or "")
    if result.ok:
        result.messages = list(messages) + [{"role": "assistant", "content": result.final_text}]
        llm._count_subscription_call()
    db.x("UPDATE agent_runs SET finished_at=?, status=?, summary=?, usd=0, searches=?, steps=? WHERE id=?",
         (db.now(), "ok" if result.ok else "error", (result.final_text or result.error)[:500], result.searches,
          db.j(result.steps[-60:]), run_id))
    return result


def note(run_id: int, summary: str) -> None:
    """Replace a run's summary with a plain account of what it achieved."""
    db.x("UPDATE agent_runs SET summary=? WHERE id=?", (summary[:500], run_id))


def log_simple(agent: str, subject: str, ok: bool, summary: str, started: float, usd: float = 0.0) -> None:
    """Record a run of an agent that is one Claude call rather than a loop (the Strategist)."""
    db.x("INSERT INTO agent_runs(agent,subject,started_at,finished_at,status,summary,usd,searches,steps) "
         "VALUES(?,?,?,?,?,?,?,?,?)", (agent, subject[:200], started, db.now(), "ok" if ok else "error",
                                       summary[:500], round(usd, 4), 0, "[]"))


def overview() -> dict:
    month_start = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))
    stats = {r["agent"]: r for r in db.q(
        "SELECT agent, COUNT(*) runs, SUM(status='ok') ok, ROUND(SUM(usd),2) usd, MAX(started_at) last FROM agent_runs "
        "WHERE started_at>=? GROUP BY agent", (month_start,))}
    return {"agents": [{"key": k, "label": v[0], "what": v[1], "on": is_on(k),
                        **{f: (stats.get(k) or {}).get(f) or 0 for f in ("runs", "ok", "usd", "last")}}
                       for k, v in AGENTS.items()],
            "usage": llm.usage_this_month(), "budget": config.AGENT_BUDGET_USD, "llm": llm.available(),
            "runs": db.q("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 40")}
