"""Re-engagement: when a lead you contacted and never heard back from shows a new signal, draft a fresh message.

A lead qualifies when it was contacted at least three weeks ago, has never replied, has a usable email, and has no
message already waiting. The draft waits in Outreach for your approval, like any other.
"""
import time

from . import agentkit
from . import brain as brain_mod
from . import db, llm

QUIET_DAYS = 21
SCHEMA = llm.obj({"subject": llm.STR, "body": llm.STR})
SYSTEM = """You write one short re-engagement email for a founder. The prospect was emailed weeks ago and never
replied. Something new just happened at their company. Open with that new thing, stated plainly; connect it to
the one problem the founder solves; ask one easy question. Under 70 words, a new subject under 6 words, plain
text with a greeting line and no signature. Do not mention that they did not reply. No exclamation marks, no
em dashes, no invented facts."""


def _eligible(lead_id: int) -> dict | None:
    lead = db.one("SELECT * FROM leads WHERE id=? AND status='contacted'", (lead_id,))
    if not lead:
        return None
    last = db.one("SELECT MAX(sent_at) t FROM messages WHERE lead_id=? AND status IN ('sent','done')", (lead_id,))["t"]
    if not last or last > time.time() - QUIET_DAYS * 86400:
        return None
    if db.one("SELECT 1 FROM replies WHERE lead_id=?", (lead_id,)) or \
            db.one("SELECT 1 FROM messages WHERE lead_id=? AND status IN ('draft','queued','sending')", (lead_id,)):
        return None
    c = db.one("SELECT * FROM contacts WHERE lead_id=? AND email IS NOT NULL AND email_status!='invalid' "
               "AND do_not_contact=0 ORDER BY is_primary DESC LIMIT 1", (lead_id,))
    if not c or db.one("SELECT 1 FROM suppression WHERE value IN (?,?)", (c["email"], c["email"].split("@")[-1])):
        return None
    return {"lead": lead, "contact": c}


def note_signal(lead_id: int) -> None:
    """Called when a new signal attaches to an existing lead."""
    if _eligible(lead_id):
        q = db.get_setting("reengage_queue", []) or []
        if lead_id not in q:
            db.set_setting("reengage_queue", (q + [lead_id])[-50:])


async def run_queue(limit: int = 5) -> int:
    q = db.get_setting("reengage_queue", []) or []
    if not q or not agentkit.is_on("reengage") or not llm.available():
        return 0
    done, rest = 0, list(q)
    for lead_id in q[:limit]:
        rest.remove(lead_id)
        found = _eligible(lead_id)
        if not found:
            continue
        lead, c = found["lead"], found["contact"]
        sig = db.one("SELECT * FROM signals WHERE lead_id=? ORDER BY created_at DESC LIMIT 1", (lead_id,))
        before = db.q("SELECT subject, body FROM messages WHERE lead_id=? AND status='sent' ORDER BY sent_at", (lead_id,))
        started, spent = time.time(), llm.usage_this_month().get("usd", 0)
        data = await llm.ask_json(
            SYSTEM + "\n\n# Company\n" + brain_mod.summary(brain_mod.get()),
            f"# Prospect\n{c['name'] or 'There'} ({c['title'] or ''}) at {lead['company_name']}\n\n# What happened just now\n"
            f"[{sig['type']}] {sig['title']}\n{(sig['text'] or '')[:400]}\n\n# What we sent before\n" +
            "\n---\n".join(f"{m['subject']}\n{m['body']}" for m in before[-2:]), SCHEMA, effort="low", max_tokens=2000)
        cost = llm.usage_this_month().get("usd", 0) - spent
        if not data:
            agentkit.log_simple("reengage", lead["company_name"] or "", False, llm.last_error or "No draft.", started, cost)
            continue
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, c["id"], "email", 1, data["subject"], data["body"], "draft", db.now(), db.now()))
        agentkit.log_simple("reengage", lead["company_name"] or "", True,
                            f"Drafted a new message to {lead['company_name']} about: {sig['title'][:120]}", started, cost)
        done += 1
    db.set_setting("reengage_queue", rest)
    return done
