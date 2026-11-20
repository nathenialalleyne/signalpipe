"""The Prospector agent: finds companies on the open web that fit your customer profile and show a buying trigger now.

It works from the play's focus, your customer profile, and the companies you approved, contacted or won (to
find lookalikes) and rejected (to avoid). Every company it records needs a source it actually read. It runs as
the play source "agent", and the Copilot can ask it for a specific kind of company.
"""
import time
from typing import TypedDict

from anthropic import beta_async_tool

from . import agentkit
from . import brain as brain_mod
from . import config, db, llm, web
from .collectors import Signal, terms


class Company(TypedDict):
    name: str
    domain: str
    trigger: str
    source_url: str
    why: str


SYSTEM = """You find new sales prospects for a founder, by searching the web like a sharp researcher.

Find companies that both:
1. fit the founder's ideal customer profile below, and
2. show a concrete, recent buying trigger with a source you read: a funding round, hiring for the listed roles,
   a launch or expansion, a new leader in a buying role, or people publicly weighing or leaving a competitor.

How to work:
- Search with specific queries (industry + trigger + recent month, competitor + "alternative", role + "hiring").
- Lookalikes: when good customers or approved leads are listed, look for companies like them.
- Record what you find with add_companies as you go, a few at a time. Aim for the target number, then stop.

Rules:
- Never invent a company, domain or source. Every company needs the source_url where you saw the trigger.
- domain: the company's own website domain, only if you saw it; otherwise an empty string.
- trigger: one line with the date if known, e.g. "Raised $12M Series A (Oct 2026)".
- why: one sentence on why it fits the profile.
- Skip the companies already known (listed below), the founder's competitors, agencies, recruiters, and anything
  matching the disqualifiers."""


def _known() -> tuple[set[str], list[str]]:
    rows = db.q("SELECT company_name, company_domain FROM leads ORDER BY id DESC LIMIT 400")
    keys = set()
    for r in rows:
        if r["company_domain"]:
            keys.add(r["company_domain"].lower())
        if r["company_name"]:
            keys.add(r["company_name"].strip().lower())
    names = [r["company_name"] for r in rows if r["company_name"]][:150]
    return keys, names


def _examples() -> str:
    good = db.q("SELECT company_name, company_domain, description FROM leads WHERE status IN ('won','replied','approved',"
                "'contacted') ORDER BY CASE status WHEN 'won' THEN 0 WHEN 'replied' THEN 1 ELSE 2 END, updated_at DESC LIMIT 12")
    bad = db.q("SELECT company_name, note FROM leads WHERE status='rejected' ORDER BY updated_at DESC LIMIT 10")
    lines = []
    if good:
        lines.append("# Good fits so far (find more like these)")
        lines += [f"- {g['company_name']} {g['company_domain'] or ''}: {(g['description'] or '')[:140]}" for g in good]
    if bad:
        lines.append("# Rejected by the founder (avoid companies like these)")
        lines += [f"- {b['company_name']}" + (f": {b['note']}" if b["note"] else "") for b in bad]
    return "\n".join(lines)


async def find(cfg: dict, instruction: str = "", target: int = 0) -> tuple[list[Signal], agentkit.Run]:
    brain = brain_mod.get()
    target = target or int(cfg.get("agent_target") or 10)
    known, names = _known()
    found: list[Signal] = []

    @beta_async_tool
    async def add_companies(companies: list[Company]) -> str:
        """Record companies that fit the profile and show a trigger. Call this as you go, with a few at a time.

        Args:
            companies: The companies, each with its source_url. Use an empty string for an unknown domain.
        """
        added, skipped = 0, 0
        for c in companies:
            name = (c.get("name") or "").strip()
            domain = web.host_of(c.get("domain") or "") if c.get("domain") else ""
            url = (c.get("source_url") or "").strip()
            if not name or not url.startswith("http") or len(name) > 80:
                skipped += 1
                continue
            if name.lower() in known or (domain and domain in known):
                skipped += 1
                continue
            known.update({name.lower(), domain} - {""})
            found.append(Signal(
                source="agent", type="agent_found", title=f"{name}: {(c.get('trigger') or '')[:200]}",
                text=(c.get("why") or "")[:600], url=url, occurred_at=time.time(), company_name=name,
                company_domain=domain, extra={"trigger": c.get("trigger", ""), "found_by": "prospector"}))
            added += 1
        return f"Added {added}, skipped {skipped} (already known or no source). {len(found)} of {target} so far."

    icp = brain.get("icp", {})
    focus = [
        f"Keywords: {', '.join(terms(cfg, 'keywords'))}", f"Competitors: {', '.join(terms(cfg, 'competitors'))}",
        f"Hiring roles that signal need: {', '.join(terms(cfg, 'hiring_titles'))}",
        f"Industries for funding news: {', '.join(terms(cfg, 'funding_keywords'))}",
        f"Disqualifiers: {', '.join(icp.get('disqualifiers', []))}",
        f"Recent means the last {int(cfg.get('lookback_days') or 14)} days. Today is {time.strftime('%Y-%m-%d')}.",
    ]
    prompt = "\n".join(["# The founder's company", brain_mod.summary(brain, 1200), "", "# This search", *focus,
                        f"Target: {target} companies.", "", _examples(), "",
                        "# Already known (skip these)", ", ".join(names) or "(none yet)"])
    if instruction:
        prompt += f"\n\n# What the founder asked for\n{instruction}"
    searches = max(3, min(12, target))
    result = await agentkit.run("prospector", system=SYSTEM, subject=instruction[:80] or "play run",
                                messages=[{"role": "user", "content": prompt + "\n\nStart searching now."}],
                                tools=[add_companies], web=agentkit.web_tools(config.LLM_MODEL, searches, 3))
    agentkit.note(result.run_id, f"Found {len(found)} new companies" + (f" ({result.error})" if result.error else "")
                  + f", {result.searches} searches.")
    return found, result


async def collect(cfg: dict) -> list[Signal]:
    """Play source "agent". Quietly does nothing without a key, when switched off, or over budget."""
    if not llm.available() or not agentkit.is_on("prospector") or llm.over_budget():
        return []
    found, _ = await find(cfg)
    return found
