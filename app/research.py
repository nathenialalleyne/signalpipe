"""The people-finding agent.

For one company, Claude researches on the open web and on the company's own site, using its web search and web
fetch tools plus three of ours, and decides for itself what to look at next. It returns the one to three people
most likely to buy, each with the evidence it found, a LinkedIn profile only if it actually saw one, and a work
email only if one was published or our checker found it. It never visits LinkedIn itself: profiles are found
through search results.
"""
import json
import logging
import re
import time
from typing import TypedDict

from anthropic import beta_async_tool

from . import agentkit
from . import brain as brain_mod
from . import config, db, enrich, llm, validate, web

log = logging.getLogger("signalpipe.research")


class Person(TypedDict):
    name: str
    title: str
    linkedin_url: str
    email: str
    why: str
    evidence: list[str]


SYSTEM = """You research one company for a founder doing outbound sales, and find the people there most likely to
buy what the founder sells. You work like a careful sales researcher: search, read, check, and only then record.

How to work:
1. If you know the company's domain, call read_company_site first. Otherwise search for the company's website.
2. Search the web for the people who hold the buyer titles listed below at this company, for example
   "<title> <company> LinkedIn" or the company name with "head of sales". Prefer recent sources, and confirm
   the person still works there (their profile headline, the company's team page, a recent article or post).
3. For each person you keep, call find_work_email with their name and the company domain, unless an address
   for them was published on a page you read.
4. Call save_people exactly once at the end, then stop. Save an empty list if you found no one you trust.

Rules:
- Record only real people you saw named in a source, with that source's URL in evidence.
- linkedin_url: only a linkedin.com/in/ address that appeared in a search result or page. Never build one from a
  name. Use an empty string otherwise.
- email: only an address that was published on a page you read, or that find_work_email returned with status
  "valid" or "risky". Never guess an address yourself. Use an empty string otherwise.
- why: one sentence on why this person is the right one to contact, given the buyer titles and the signal.
- Do not open linkedin.com pages directly; work from search results.
- Use public, professional information only. Nothing about anyone's private life.
- Keep it short: a handful of searches is usually enough. Stop as soon as you have one or two strong people."""


def _brief(lead: dict, signals: list[dict], contacts: list[dict], brain: dict) -> str:
    icp = brain.get("icp", {})
    lines = [
        "# The founder's company", brain_mod.summary(brain, 900),
        "", "# Buyer titles to look for (most likely first)", ", ".join(icp.get("titles") or ["Founder", "CEO", "Head of Sales"]),
        "", "# Company to research",
        f"Name: {lead.get('company_name') or '(unknown)'}",
        f"Domain: {lead.get('company_domain') or '(unknown; find it)'}",
        f"About: {(lead.get('description') or '')[:400]}",
        "", "# Why it surfaced (signals, newest first)",
    ]
    for s in signals[:3]:
        lines.append(f"- [{s['type']}] {s['title'][:200]} {s.get('url') or ''}")
    named = [c for c in contacts if c.get("name")]
    if named:
        lines += ["", "# Already known (check they still fit, add better ones if you find them)"]
        lines += [f"- {c['name']}, {c.get('title') or ''}" for c in named[:5]]
    return "\n".join(lines)


async def research_lead(lead_id: int, force: bool = False) -> dict:
    """Run the agent for one lead and store what it found. Returns a short report."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        return {"error": "no such lead"}
    signals = db.q("SELECT * FROM signals WHERE lead_id=? ORDER BY occurred_at DESC", (lead_id,))
    contacts = db.q("SELECT * FROM contacts WHERE lead_id=?", (lead_id,))
    brain = brain_mod.get()
    found: dict = {}
    calls = {"read_company_site": 0, "find_work_email": 0}

    @beta_async_tool
    async def read_company_site(domain: str) -> str:
        """Read a company's own website: its about, team, leadership and contact pages. Returns the named people
        with titles found there, published email addresses and phone numbers, the LinkedIn company page and a
        description. Call this first whenever you know the company's domain.

        Args:
            domain: The company's website domain, for example acme.com.
        """
        calls["read_company_site"] += 1
        site = await enrich.crawl_company(web.host_of(domain), use_llm=False)
        if not site["ok"]:
            return json.dumps({"ok": False, "note": "The site could not be read."})
        return json.dumps({
            "ok": True, "title": site["title"], "description": site["description"], "people": site["people"][:15],
            "emails": site["emails"][:8], "phones": [p["display"] for p in site["phones"][:3]],
            "linkedin_company_page": site["linkedin_company_url"], "pages_read": site["pages"],
        })[:12000]

    @beta_async_tool
    async def find_work_email(name: str, domain: str) -> str:
        """Find and check a named person's work email at a company domain, using the company's known address format
        and its mail server where possible. Call this for each person you plan to record who has no published
        address. Use only what it returns; never invent an address.

        Args:
            name: The person's full name, for example Dana Reyes.
            domain: The company's email domain, for example acme.com.
        """
        calls["find_work_email"] += 1
        r = await validate.find_email(name, web.host_of(domain))
        return json.dumps({"email": r.get("email", ""), "status": r.get("status"), "reason": r.get("reason", "")})

    @beta_async_tool
    async def save_people(people: list[Person], summary: str, hooks: list[str]) -> str:
        """Record your findings. Call this exactly once, at the end, even when you found no one (pass an empty list).

        Args:
            people: Up to 3 people, the best buyer first. Use empty strings for anything you do not know.
            summary: Two or three sentences on what this company is doing now, from what you read.
            hooks: Up to 3 specific, checkable things worth mentioning when reaching out, each ending with its source URL.
        """
        found.update(people=people[:3], summary=summary, hooks=hooks[:3])
        return "Saved. You are done: reply with one short sentence and stop."

    started = time.time()
    result = await agentkit.run(
        "people", system=SYSTEM, subject=lead.get("company_name") or lead.get("company_domain") or f"lead {lead_id}",
        messages=[{"role": "user", "content": _brief(lead, signals, contacts, brain) +
                   "\n\nResearch this company now and finish by calling save_people."}],
        tools=[read_company_site, find_work_email, save_people], web=agentkit.web_tools(config.LLM_MODEL, 6, 5),
        force=force)
    if not found:
        return {"error": result.error or "The agent finished without recording anyone. Try again, or add contacts by hand."}
    out = await _store(lead, found, config.LLM_MODEL, result.searches, calls, time.time() - started)
    agentkit.note(result.run_id, f"Found {out['people']} people at {lead.get('company_name') or 'the company'} "
                                 f"({out['added']} new), {result.searches} searches.")
    return out


LI_PROFILE = re.compile(r"^https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/[^/?#\s]+", re.I)


async def _store(lead: dict, found: dict, model: str, searches: int, calls: dict, seconds: float) -> dict:
    lead_id = lead["id"]
    domain = lead["company_domain"] or ""
    existing = {(c["name"] or "").strip().lower(): c for c in db.q("SELECT * FROM contacts WHERE lead_id=?", (lead_id,))}
    kept, added = [], 0
    for p in found.get("people", []):
        name = (p.get("name") or "").strip()
        if not name or len(name) > 60:
            continue
        m = LI_PROFILE.match((p.get("linkedin_url") or "").strip())
        li = m.group(0) if m else ""
        evidence = [u for u in (p.get("evidence") or []) if isinstance(u, str) and u.startswith("http")][:4]
        email = validate.normalize_email(p.get("email") or "")
        detail: dict = {"status": None}
        if email:
            detail = await validate.validate_email(email)
            if detail["status"] == "invalid":
                email, detail = "", {"status": None}
            else:
                detail["flags"] = detail.get("flags", []) + ["found_by_agent"]
        person = {"name": name, "title": (p.get("title") or "")[:160], "linkedin_url": li, "email": email,
                  "why": (p.get("why") or "")[:300], "evidence": evidence}
        kept.append(person)
        old = existing.get(name.lower())
        if old:  # fill in what we did not have; never overwrite what is already there
            db.x("UPDATE contacts SET title=COALESCE(NULLIF(title,''),?), "
                 "linkedin_url=CASE WHEN ? != '' AND (linkedin_url IS NULL OR linkedin_url='' OR linkedin_url LIKE '%/search/%') "
                 "THEN ? ELSE linkedin_url END, "
                 "email=COALESCE(email, NULLIF(?,'')), email_status=CASE WHEN email IS NULL AND ? != '' THEN ? ELSE email_status END, "
                 "email_detail=CASE WHEN email IS NULL AND ? != '' THEN ? ELSE email_detail END WHERE id=?",
                 (person["title"], li, li, email, email, detail.get("status"), email, db.j(detail), old["id"]))
        else:
            db.x("INSERT INTO contacts(lead_id,name,title,email,email_status,email_detail,linkedin_url,source,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (lead_id, name, person["title"], email or None, detail.get("status"), db.j(detail),
                  li or enrich.linkedin_search_url(lead["company_name"] or domain, person["title"], name), "research", db.now()))
            added += 1
    enrich.set_primary(lead_id)
    report = {"at": db.now(), "model": model, "summary": (found.get("summary") or "")[:800],
              "hooks": [h[:300] for h in found.get("hooks", []) if isinstance(h, str)][:3], "people": kept,
              "searches": searches, "site_reads": calls["read_company_site"], "email_checks": calls["find_work_email"],
              "seconds": round(seconds, 1)}
    db.x("UPDATE leads SET research=?, updated_at=? WHERE id=?", (db.j(report), db.now(), lead_id))
    return {"people": len(kept), "added": added, "searches": searches}


async def research_top(play_id: int, limit: int, min_score: float) -> int:
    """Research the best unresearched new leads of a play. Returns how many were researched."""
    if limit <= 0 or not llm.available() or not agentkit.is_on("people"):
        return 0
    done = 0
    for row in db.q("SELECT id FROM leads WHERE play_id=? AND status='new' AND research IS NULL AND score>=? "
                    "ORDER BY score DESC LIMIT ?", (play_id, min_score, limit)):
        try:
            res = await research_lead(row["id"])
        except Exception:
            log.exception("research failed for lead %s", row["id"])
            continue
        if res.get("error"):
            log.warning("research for lead %s: %s", row["id"], res["error"])
            if any(w in res["error"] for w in ("API key", "rate limiting", "budget", "switched off")):
                break  # no point trying the rest now
            continue
        done += 1
    return done
