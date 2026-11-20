"""The Copilot: a chat that runs Signalpipe for you.

Ask in plain words ("find 20 fintech companies hiring SDRs in New York", "research the top five", "why are replies
low?") and it uses the same actions as the dashboard, plus the other agents and web search. It cannot send
anything: sending stays under your rules in Outreach. The conversation is kept until you start a new one.
"""
import asyncio
import json
import time
from typing import Literal

from anthropic import beta_async_tool

from . import actions, agentkit
from . import brain as brain_mod
from . import config, db, enrich, insights, pipeline, prospector, research, scoring

SYSTEM = """You are the copilot inside Signalpipe, a founder's outbound prospecting tool. You act on their requests
with your tools, and keep your answers short and concrete.

What you can do: search and inspect leads, find new companies with the Prospector, research a company's people,
approve or reject leads (approving drafts their sequence; nothing is sent until the founder's rules allow), create
and edit plays and run them, read results, and search the web.

Rules:
- Before approving or rejecting more than 5 leads at once, or deleting anything, say what you will do and wait.
- You cannot send emails or LinkedIn messages; tell the founder to use Outreach for that.
- When you mention a lead, give its company name and id, like "Acme (#12)".
- Do not invent facts about companies or people; say what you found and where."""

HISTORY_LIMIT = 80


def _lead_line(l: dict) -> dict:
    c = db.one("SELECT name, title, email, email_status, linkedin_url FROM contacts WHERE lead_id=? ORDER BY is_primary DESC LIMIT 1", (l["id"],))
    sig = db.one("SELECT type, title FROM signals WHERE lead_id=? ORDER BY occurred_at DESC LIMIT 1", (l["id"],))
    return {"id": l["id"], "company": l["company_name"], "domain": l["company_domain"], "status": l["status"],
            "score": l["score"], "signal": sig and f"{sig['type']}: {sig['title'][:120]}",
            "contact": c and {k: c[k] for k in ("name", "title", "email", "email_status")}, "researched": l.get("research") is not None}


def _tools() -> list:
    @beta_async_tool
    async def search_leads(text: str = "", status: Literal["new", "approved", "contacted", "replied", "won", "lost", "rejected", "all"] = "new",
                           min_score: int = 0, limit: int = 15) -> str:
        """Find leads by company name or domain, status and minimum score, best first.

        Args:
            text: Part of a company name or domain, or empty for all.
            status: Lead status, or all.
            min_score: Lowest score to include (0 to 100).
            limit: How many to return, at most 50.
        """
        where, params = ["score>=?"], [min_score]
        if status != "all":
            where.append("status=?")
            params.append(status)
        if text:
            where.append("(company_name LIKE ? OR company_domain LIKE ?)")
            params += [f"%{text}%", f"%{text}%"]
        rows = db.q(f"SELECT * FROM leads WHERE {' AND '.join(where)} ORDER BY score DESC LIMIT ?", params + [min(limit, 50)])
        return json.dumps([_lead_line(r) for r in rows], default=str)

    @beta_async_tool
    async def get_lead(lead_id: int) -> str:
        """Everything about one lead: signals, contacts, research, drafted messages and replies.

        Args:
            lead_id: The lead's id.
        """
        l = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not l:
            return "No such lead."
        return json.dumps({**_lead_line(l), "description": l["description"], "reasons": l["reasons"], "research": l["research"],
                           "signals": db.q("SELECT type, title, url FROM signals WHERE lead_id=? ORDER BY occurred_at DESC LIMIT 5", (lead_id,)),
                           "contacts": db.q("SELECT name, title, email, email_status, phone, linkedin_url FROM contacts WHERE lead_id=?", (lead_id,)),
                           "messages": db.q("SELECT channel, step, status, subject FROM messages WHERE lead_id=?", (lead_id,)),
                           "replies": db.q("SELECT kind, body FROM replies WHERE lead_id=?", (lead_id,))}, default=str)[:12000]

    @beta_async_tool
    async def research_people(lead_id: int) -> str:
        """Run the People finder on one lead: finds the right named buyers with LinkedIn profiles and checked emails.

        Args:
            lead_id: The lead's id.
        """
        return json.dumps(await research.research_lead(lead_id, force=True))

    @beta_async_tool
    async def approve_lead(lead_id: int, note: str = "") -> str:
        """Approve a lead: researches its people if needed and drafts its sequence. Nothing is sent by this.

        Args:
            lead_id: The lead's id.
            note: Optional note on why.
        """
        return json.dumps(await actions.approve_lead(lead_id, note or None), default=str)

    @beta_async_tool
    async def reject_lead(lead_id: int, note: str) -> str:
        """Reject a lead. The note teaches future scoring what the founder does not want.

        Args:
            lead_id: The lead's id.
            note: Why, in a few words.
        """
        return json.dumps(actions.reject_lead(lead_id, note))

    @beta_async_tool
    async def list_plays() -> str:
        """All plays with their settings and lead counts."""
        out = []
        for p in db.q("SELECT * FROM plays ORDER BY id"):
            counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM leads WHERE play_id=? GROUP BY status", (p["id"],))}
            out.append({"id": p["id"], "name": p["name"], "status": p["status"], "counts": counts,
                        **{k: p["config"].get(k) for k in ("sources", "keywords", "competitors", "hiring_titles", "funding_keywords", "watchlist")}})
        return json.dumps(out)

    @beta_async_tool
    async def save_play(name: str, play_id: int = 0, keywords: list[str] | None = None, competitors: list[str] | None = None,
                        hiring_titles: list[str] | None = None, funding_keywords: list[str] | None = None,
                        watchlist: list[str] | None = None) -> str:
        """Create a play (play_id 0) or change an existing one. Lists you pass replace the play's current lists.

        Args:
            name: The play's name.
            play_id: 0 to create a new play, or the id of the play to change.
            keywords: Pain keywords prospects use.
            competitors: Competitor names to watch for.
            hiring_titles: Job titles whose ads signal need.
            funding_keywords: Industries to watch for funding news.
            watchlist: Company domains whose job boards to watch.
        """
        changes = {k: v for k, v in dict(keywords=keywords, competitors=competitors, hiring_titles=hiring_titles,
                                          funding_keywords=funding_keywords, watchlist=watchlist).items() if v is not None}
        if play_id:
            p = db.one("SELECT * FROM plays WHERE id=?", (play_id,))
            if not p:
                return "No such play."
            db.x("UPDATE plays SET name=?, config=? WHERE id=?", (name or p["name"], db.j({**p["config"], **changes}), play_id))
            return f"Play {play_id} updated."
        cfg = {**brain_mod.suggested_play(brain_mod.get()), **changes}
        pid = db.x("INSERT INTO plays(name,config,created_at) VALUES(?,?,?)", (name, db.j(cfg), db.now()))
        return f"Created play {pid}."

    @beta_async_tool
    async def run_play(play_id: int) -> str:
        """Start a full run of a play in the background: collects signals, finds people, scores. Takes a few minutes.

        Args:
            play_id: The play's id.
        """
        if not db.one("SELECT 1 FROM plays WHERE id=?", (play_id,)):
            return "No such play."
        asyncio.get_running_loop().create_task(pipeline.run_play(play_id))
        return "Started. New leads appear on the Leads page as it goes."

    @beta_async_tool
    async def find_companies(play_id: int, what: str, count: int = 10) -> str:
        """Ask the Prospector to search the web for a specific kind of company, add them to a play as new leads, and
        look up and score them.

        Args:
            play_id: The play to add them to.
            what: What to look for, in the founder's words, e.g. "fintech startups in New York hiring SDRs".
            count: How many to aim for, at most 25.
        """
        p = db.one("SELECT * FROM plays WHERE id=?", (play_id,))
        if not p:
            return "No such play. Create one with save_play first."
        found, run = await prospector.find(p["config"], instruction=what, target=min(max(count, 1), 25))
        _, new_leads, touched = pipeline.ingest(play_id, found)
        for lid in list(touched)[:25]:
            try:
                await enrich.enrich_lead(lid)
            except Exception:
                pass
        await scoring.score_leads(list(touched))
        rows = db.q("SELECT * FROM leads WHERE id IN (%s) ORDER BY score DESC" % (",".join(str(i) for i in touched) or "0"))
        return json.dumps({"added": new_leads, "note": run.error or "", "leads": [_lead_line(r) for r in rows]}, default=str)[:12000]

    @beta_async_tool
    async def results(play_id: int = 0) -> str:
        """What is working: the funnel and reply rates by opening, signal, buyer level, score and channel.

        Args:
            play_id: A play's id, or 0 for all plays.
        """
        r = insights.report(play_id or None)
        return json.dumps({"funnel": r["funnel"], "findings": r["findings"],
                           "tables": {k: [{f: g[f] for f in ("name", "contacted", "replied", "reply_rate", "positive", "won")} for g in v]
                                      for k, v in r["tables"].items()}})

    @beta_async_tool
    async def company_profile() -> str:
        """The founder's company profile (the Brain): what they sell, ideal customer, pains, competitors."""
        return brain_mod.summary(brain_mod.get(), 3000)

    return [search_leads, get_lead, research_people, approve_lead, reject_lead, list_plays, save_play, run_play,
            find_companies, results, company_profile]


def transcript() -> list:
    return (db.get_setting("copilot", {}) or {}).get("log", [])


def reset() -> None:
    db.set_setting("copilot", {"messages": [], "log": []})


async def ask(text: str) -> dict:
    state = db.get_setting("copilot", {}) or {}
    history, log = state.get("messages", []), state.get("log", [])
    if len(history) > HISTORY_LIMIT:  # start afresh rather than cut a conversation in the middle
        history, log = [], log + [{"role": "note", "text": "Started a new conversation to keep it fast.", "at": time.time()}]
    log.append({"role": "you", "text": text, "at": time.time()})
    session = state.get("cc_session", "") if history else ""
    result = await agentkit.run("copilot", system=SYSTEM + f"\nToday is {time.strftime('%A %Y-%m-%d')}.", subject=text[:120],
                                messages=history + [{"role": "user", "content": text}], tools=_tools(),
                                web=agentkit.web_tools(config.LLM_MODEL, 5, 3), max_turns=16, resume=session)
    reply = result.final_text or result.error or "Done."
    log.append({"role": "copilot", "text": reply, "steps": [s["tool"] for s in result.steps], "at": time.time(),
                "error": not result.ok})
    db.set_setting("copilot", {"messages": result.messages if result.ok else history, "log": log[-200:],
                               "cc_session": (result.session_id or session) if result.ok else session})
    return {"reply": reply, "ok": result.ok, "steps": result.steps}
