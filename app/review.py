"""The weekly review agent: reads what actually happened and proposes changes, which you apply with one click.

It looks at each play's results by signal, buyer level and opening, at what people wrote back, and at your
rejection notes. It suggests keywords, competitors and hiring titles to add or drop, buyer titles and
disqualifiers to change, and short notes that shape future messages. Nothing changes until you apply it.
"""
import time
import uuid

from . import brain as brain_mod
from . import db, insights, llm

KINDS = ["add_keyword", "remove_keyword", "add_competitor", "remove_competitor", "add_hiring_title",
         "remove_hiring_title", "add_buyer_title", "remove_buyer_title", "add_disqualifier", "messaging_note"]
PLAY_FIELD = {"keyword": "keywords", "competitor": "competitors", "hiring_title": "hiring_titles"}
BRAIN_FIELD = {"buyer_title": "titles", "disqualifier": "disqualifiers"}
WEEK = 7 * 86400

SCHEMA = llm.obj({"suggestions": llm.arr(llm.obj({
    "kind": {"type": "string", "enum": KINDS}, "play_id": llm.INT, "value": llm.STR, "why": llm.STR}))})

SYSTEM = """You review a founder's outbound prospecting and propose changes that will get more replies from the
right buyers. You get each play's settings and results, what prospects wrote back, and the founder's notes on
leads they rejected.

Propose at most 6 changes, the most useful first. Each must be grounded in the data below: in `why`, cite the
numbers or quote a few words from a reply or a rejection note. With little data, propose fewer, or none; an
empty list is a good answer when nothing is clear yet.

- add_/remove_keyword, add_/remove_competitor, add_/remove_hiring_title change one play: set play_id to it.
- add_/remove_buyer_title and add_disqualifier change the company profile used everywhere: set play_id to 0.
- messaging_note: one short instruction for future messages learned from replies, for example "Lead with setup
  time; three replies asked how long it takes." play_id 0.
- value: the exact keyword, name, title or note. For a removal, copy the existing value exactly."""


def _evidence() -> str:
    brain = brain_mod.get()
    lines = ["# Company profile", brain_mod.summary(brain, 1200),
             f"Buyer titles: {', '.join(brain['icp'].get('titles', []))}",
             f"Disqualifiers: {', '.join(brain['icp'].get('disqualifiers', []))}",
             f"Current message notes: {'; '.join(brain.get('message_notes', [])) or '(none)'}"]
    for play in db.q("SELECT * FROM plays WHERE status='active'"):
        cfg, rep = play["config"], insights.report(play["id"])
        f = rep["funnel"]
        lines += ["", f"# Play {play['id']}: {play['name']}",
                  f"Keywords: {', '.join(cfg.get('keywords', []))}",
                  f"Competitors: {', '.join(cfg.get('competitors', []))}",
                  f"Hiring titles: {', '.join(cfg.get('hiring_titles', []))}",
                  f"Funnel: found {f['found']}, approved {f['approved']} of {f['reviewed']} reviewed, contacted "
                  f"{f['contacted']}, replied {f['replied']}, interested {f['positive']}, won {f['won']}"]
        for key, label in (("signal", "By signal"), ("title", "By buyer level"), ("angle", "By opening")):
            rows = [f"{g['name']}: {g['contacted']} contacted, {g['replied']} replied, {g['positive']} interested"
                    for g in rep["tables"][key]]
            if rows:
                lines.append(f"{label}: " + "; ".join(rows))
        terms = db.q("SELECT json_extract(s.extra,'$.term') term, l.status, COUNT(*) n FROM signals s JOIN leads l ON "
                     "l.id=s.lead_id WHERE s.play_id=? AND json_extract(s.extra,'$.term') IS NOT NULL GROUP BY 1,2", (play["id"],))
        by_term: dict[str, dict] = {}
        for t in terms:
            by_term.setdefault(t["term"], {})[t["status"]] = t["n"]
        if by_term:
            lines.append("Leads by matching term (status counts): " + "; ".join(
                f"{t}: {dict(sorted(v.items()))}" for t, v in sorted(by_term.items())))
    replies = db.q("SELECT r.kind, r.body, l.company_name, (SELECT type FROM signals s WHERE s.lead_id=l.id ORDER BY "
                   "occurred_at DESC LIMIT 1) sig FROM replies r JOIN leads l ON l.id=r.lead_id WHERE r.kind LIKE 'reply%' "
                   "ORDER BY r.received_at DESC LIMIT 30")
    if replies:
        lines += ["", "# What prospects wrote back (newest first)"]
        lines += [f"- [{r['kind']}, signal {r['sig']}] {r['company_name']}: {(r['body'] or '')[:280]}" for r in replies]
    rejected = db.q("SELECT l.company_name, l.note, (SELECT title FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at "
                    "DESC LIMIT 1) sig FROM leads l WHERE l.status='rejected' ORDER BY l.updated_at DESC LIMIT 30")
    if rejected:
        lines += ["", "# Leads the founder rejected (newest first)"]
        lines += [f"- {r['company_name']}: {r['sig'] or ''}" + (f" | note: {r['note']}" if r["note"] else "") for r in rejected]
    return "\n".join(lines)


def enough_data() -> bool:
    n = db.one("SELECT SUM(status IN ('contacted','replied','won','lost')) c, SUM(status='rejected') r FROM leads")
    return (n["c"] or 0) >= 10 or (n["r"] or 0) >= 10


def due() -> bool:
    return enough_data() and time.time() - (db.get_setting("review") or {}).get("at", 0) > WEEK


def state() -> dict:
    r = db.get_setting("review") or {}
    return {"at": r.get("at", 0), "suggestions": [s for s in r.get("suggestions", []) if s.get("status") == "open"],
            "enough_data": enough_data(), "llm": llm.available()}


async def run(force: bool = False) -> dict:
    from . import agentkit
    if not llm.available():
        return {"error": "Connect Claude in Settings to use the review."}
    if not force and not agentkit.is_on("strategist"):
        return {"error": "The Strategist is switched off on the Agents page."}
    started, spent = time.time(), llm.usage_this_month().get("usd", 0)
    data = await llm.ask_json(SYSTEM, _evidence(), SCHEMA, effort="medium", max_tokens=8000)
    cost = llm.usage_this_month().get("usd", 0) - spent
    if data is None:
        agentkit.log_simple("strategist", "Weekly review", False, llm.last_error or "No answer.", started, cost)
        return {"error": llm.last_error or "The review did not return anything."}
    plays = {p["id"] for p in db.q("SELECT id FROM plays")}
    kept = []
    for s in data.get("suggestions", [])[:6]:
        target = s["kind"].split("_", 1)[1] if "_" in s["kind"] else ""
        if s["kind"] != "messaging_note" and target in PLAY_FIELD and s["play_id"] not in plays:
            continue
        if not s["value"].strip():
            continue
        kept.append({**s, "value": s["value"].strip()[:200], "id": uuid.uuid4().hex[:10], "status": "open"})
    db.set_setting("review", {"at": time.time(), "suggestions": kept})
    agentkit.log_simple("strategist", "Weekly review", True,
                        f"Proposed {len(kept)} change{'s' if len(kept) != 1 else ''}: " +
                        "; ".join(f"{s['kind'].replace('_', ' ')} \"{s['value'][:40]}\"" for s in kept[:4]), started, cost)
    return {"suggestions": len(kept)}


def _edit(items: list, value: str, add: bool) -> list:
    low = value.lower()
    rest = [x for x in items if x.lower() != low]
    return rest + [value] if add else rest


def decide(sid: str, apply: bool) -> dict:
    r = db.get_setting("review") or {}
    s = next((x for x in r.get("suggestions", []) if x["id"] == sid), None)
    if not s or s["status"] != "open":
        return {"error": "That suggestion is no longer open."}
    if apply:
        kind, value = s["kind"], s["value"]
        if kind == "messaging_note":
            b = brain_mod.get()
            brain_mod.save({"message_notes": (b.get("message_notes", []) + [value])[-8:]})
        else:
            verb, target = kind.split("_", 1)
            if target in PLAY_FIELD:
                play = db.one("SELECT * FROM plays WHERE id=?", (s["play_id"],))
                if not play:
                    return {"error": "That play no longer exists."}
                cfg = play["config"]
                cfg[PLAY_FIELD[target]] = _edit(cfg.get(PLAY_FIELD[target], []), value, verb == "add")
                db.x("UPDATE plays SET config=? WHERE id=?", (db.j(cfg), play["id"]))
            else:
                b = brain_mod.get()
                field = BRAIN_FIELD[target]
                brain_mod.save({"icp": {field: _edit(b["icp"].get(field, []), value, verb == "add")}})
    s["status"] = "applied" if apply else "dismissed"
    db.set_setting("review", r)
    return {"ok": True, "status": s["status"]}
