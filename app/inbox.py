"""The Inbox agent: handles a reply by acting on it, not only by drafting an answer.

It can schedule a follow-up for when the person said to come back, add someone they referred you to (with an
intro draft), record a booked meeting or a no, honour an unsubscribe, and ask you a question it cannot answer
from your company profile. Your answer goes into the profile's Q&A, so the next reply that asks gets it right.
It never sends anything itself: replies and new messages are drafts for you, as before.
"""
from typing import Literal

from anthropic import beta_async_tool

from . import agentkit
from . import brain as brain_mod
from . import db, validate

INTENTS = Literal["interested", "question", "not_now", "not_interested", "referral", "unsubscribe", "auto_reply"]

SYSTEM = """You handle replies to a founder's outbound messages, the way a sharp assistant would.

Read the reply, then:
- Take every action it calls for with your tools: a later date for a follow-up, a referral to someone else, a
  booked meeting, a clear no, a request to stop.
- If they ask something the company profile and its Q&A below do not answer, call ask_founder with the question,
  and put a bracketed placeholder in your draft where the answer goes. Never invent prices, features or facts.
- Finish by calling save_reply once, with the intent and the founder's answer: short, plain, human, responsive to
  exactly what they asked, with one clear next step. If they said no, one gracious line. For an auto-reply or an
  unsubscribe, an empty draft is fine.
No exclamation marks, no em dashes."""


def faq_text(brain: dict) -> str:
    answered = [f"Q: {f['q']}\nA: {f['a']}" for f in brain.get("faq", []) if f.get("a")]
    return "\n".join(answered) or "(none yet)"


async def handle(lead: dict, contact: dict, sent: list[dict], body: str, sender: str) -> dict | None:
    """Returns {"intent", "draft", "actions"} or None to let the caller fall back to the single-call version."""
    from . import outreach
    lead_id = lead["id"]
    out: dict = {"actions": []}

    @beta_async_tool
    async def schedule_follow_up(days: int, message: str) -> str:
        """Schedule a follow-up email for when they asked you to come back, for example "try me in March".

        Args:
            days: How many days from today to send it (1 to 270).
            message: The follow-up email's body, written now, referring back to this exchange.
        """
        days = max(1, min(int(days), 270))
        last = sent[-1]["subject"] if sent else "following up"
        subject = last if last.lower().startswith("re:") else f"Re: {last}"
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, contact.get("id"), "email", 1, subject, message, "draft",
                                           db.now() + days * 86400, db.now()))
        db.x("UPDATE leads SET status='contacted', updated_at=? WHERE id=?", (db.now(), lead_id))
        out["actions"].append(f"Follow-up drafted for {days} days from now")
        return f"Follow-up drafted for {days} days from now. It waits for the founder's approval in Outreach."

    @beta_async_tool
    async def add_referral(name: str, title: str, email: str, intro_message: str) -> str:
        """Add a person they referred you to, with an intro email to that person for the founder to review.

        Args:
            name: Their full name.
            title: Their job title, or an empty string.
            email: Their email if it was given in the reply, or an empty string. Never guess one.
            intro_message: An intro email to them that mentions who referred you.
        """
        addr = validate.normalize_email(email)
        detail = await validate.validate_email(addr) if addr else {"status": None}
        cid = db.x("INSERT INTO contacts(lead_id,name,title,email,email_status,email_detail,source,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (lead_id, name, title, addr or None, detail.get("status"),
                                               db.j(detail), "referral", db.now()))
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, cid, "email", 1, f"{(contact.get('name') or 'A colleague').split()[0]} suggested I reach out",
                                           intro_message, "draft", db.now(), db.now()))
        out["actions"].append(f"Added referral {name}" + (" with an intro draft" if addr else "; needs an email address"))
        return f"Added {name}" + ("" if addr else "; no email yet, so the founder will add one") + ". Intro drafted."

    @beta_async_tool
    async def record_outcome(outcome: Literal["meeting_booked", "not_interested"], note: str) -> str:
        """Record that a meeting was booked, or that they are not interested. Stops the rest of the sequence.

        Args:
            outcome: meeting_booked or not_interested.
            note: A few words on why, from the reply.
        """
        outreach.stop_sequence(lead_id, outcome.replace("_", " "))
        db.x("UPDATE leads SET status=?, note=COALESCE(note, ?), updated_at=? WHERE id=?",
             ("won" if outcome == "meeting_booked" else "lost", note[:200], db.now(), lead_id))
        out["actions"].append("Marked as meeting booked" if outcome == "meeting_booked" else "Marked as not interested")
        return "Recorded."

    @beta_async_tool
    async def unsubscribe(reason: str) -> str:
        """They asked not to be contacted again. Adds them to the do-not-contact list and stops everything.

        Args:
            reason: Their words, briefly.
        """
        if contact.get("email"):
            db.x("INSERT OR IGNORE INTO suppression(value,reason,created_at) VALUES(?,?,?)", (contact["email"], reason[:120], db.now()))
        if contact.get("id"):
            db.x("UPDATE contacts SET do_not_contact=1 WHERE id=?", (contact["id"],))
        outreach.stop_sequence(lead_id, "unsubscribed")
        db.x("UPDATE leads SET status='lost', updated_at=? WHERE id=?", (db.now(), lead_id))
        out["actions"].append("Unsubscribed and added to do-not-contact")
        return "Done. Nothing more will be sent to them."

    @beta_async_tool
    async def ask_founder(question: str) -> str:
        """Ask the founder a question the profile cannot answer (pricing, a feature, a policy). It is added to the
        profile's Q&A for the founder to answer once; future replies will use the answer.

        Args:
            question: The question, phrased generally, for example "Do you integrate with Salesforce?"
        """
        b = brain_mod.get()
        faq = b.get("faq", [])
        if not any(f["q"].strip().lower() == question.strip().lower() for f in faq):
            brain_mod.save({"faq": (faq + [{"q": question.strip()[:300], "a": ""}])[-40:]})
        out["actions"].append(f"Asked you: {question[:120]}")
        return "Added to the founder's questions. Use a bracketed placeholder for the answer in your draft."

    @beta_async_tool
    async def save_reply(intent: INTENTS, draft: str) -> str:
        """Save the reply's intent and the founder's answer. Call this once, last.

        Args:
            intent: What the reply is.
            draft: The founder's answer, or an empty string if none is needed.
        """
        out.update(intent=intent, draft=draft)
        return "Saved."

    brain = brain_mod.get()
    system = (SYSTEM + "\n\n# Company\n" + brain_mod.summary(brain) +
              (f"\nCalendar link: {brain['calendar_link']}" if brain.get("calendar_link") else "") +
              "\n\n# Q&A the founder has answered\n" + faq_text(brain))
    prompt = ("# What we sent\n" + "\n---\n".join(f"{m['subject']}\n{m['body']}" for m in sent[-2:]) +
              f"\n\n# Their reply (from {contact.get('name') or sender} at {lead.get('company_name')})\n{body}")
    result = await agentkit.run("inbox", system=system, subject=f"Reply from {lead.get('company_name') or sender}",
                                messages=[{"role": "user", "content": prompt}],
                                tools=[schedule_follow_up, add_referral, record_outcome, unsubscribe, ask_founder, save_reply],
                                max_turns=8)
    if "intent" not in out:
        return None
    agentkit.note(result.run_id, f"{out['intent'].replace('_', ' ').capitalize()} from {lead.get('company_name')}"
                  + (": " + "; ".join(out["actions"]) if out["actions"] else ""))
    return out
