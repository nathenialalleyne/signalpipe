"""Sequences across three channels.

- email: sent from your own mailbox over SMTP, with a daily cap, suppression list and unsubscribe footer.
- linkedin: drafted here, sent by you. The app queues the profile link and the note; you click send.
- call: a script per lead and a dial list ordered by who is in business hours right now.

Replies are read over IMAP: a reply stops the sequence, a bounce marks the address invalid, an
unsubscribe request goes on the suppression list.
"""
import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage

from . import brain as brain_mod
from . import config, db, llm, validate

log = logging.getLogger("signalpipe.outreach")
EMAIL_STEP_DAYS = {1: 0, 2: 3, 3: 7}
LINKEDIN_NOTE_MAX = int(config.env("LINKEDIN_NOTE_MAX", "200") or 200)  # 200 is safe everywhere; paid accounts allow 300
UNSUB_RE = re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting)|do not contact|opt.?out)\b", re.I)
OOO_RE = re.compile(r"\b(out of (the )?office|on (annual |parental )?leave|automatic reply|auto.?reply|away until)\b", re.I)

SEQ_SCHEMA = llm.obj({
    "emails": llm.arr(llm.obj({"subject": llm.STR, "body": llm.STR})),
    "linkedin_note": llm.STR,
    "linkedin_followup": llm.STR,
    "call": llm.obj({"opener": llm.STR, "reason": llm.STR, "questions": llm.STRS, "voicemail": llm.STR}),
})

SEQ_SYSTEM = """You write outbound sales messages for the company described below, to one specific prospect,
based on a real, public signal about them. The sender is a founder writing personally, not a marketing team.

What makes these work:
- Open with the signal itself, stated plainly and accurately, so the reader sees at once why they in particular
  are getting this. Never imply knowledge you do not have, and never invent a detail about them.
- One idea per message. Connect the signal to one problem the company solves, offer one concrete proof point if
  the company profile has a real one, and ask one low-effort question.
- Plain words, short sentences, no hype, no flattery, no "I hope this finds you well", no "quick question",
  no exclamation marks, no emoji, no em dashes.
- If the prospect's name is unknown, write so the message still reads naturally without a greeting name.

Produce:
- emails: exactly 3. Email 1 under 90 words. Email 2 (sent 3 days later, same thread) under 60 words and adds a
  new angle or proof rather than "bumping". Email 3 (day 7) under 50 words, a polite close-the-loop that makes
  it easy to say no. Subjects under 6 words, lowercase-style, no clickbait; emails 2 and 3 reuse email 1's subject
  prefixed with "Re: ". Bodies are plain text with a greeting line and no signature (one is appended).
- linkedin_note: a connection request note, at most 200 characters, no pitch, just the relevant reason to connect.
- linkedin_followup: the message to send after they accept, under 60 words.
- call: opener (one sentence that says who you are and asks for 30 seconds), reason (two sentences tying the
  signal to why you called), questions (2 or 3 open questions that qualify), voicemail (under 35 words)."""


def _context(lead: dict, contact: dict | None, signals: list[dict], brain: dict) -> str:
    c = contact or {}
    lines = [
        "# Prospect",
        f"Company: {lead.get('company_name') or '(unknown)'} {('(' + lead['company_domain'] + ')') if lead.get('company_domain') else ''}",
        f"About them: {lead.get('description') or '(no description found)'}",
        f"Contact: {c.get('name') or '(name unknown)'}, {c.get('title') or '(title unknown)'}",
    ]
    if lead.get("tech"):
        lines.append(f"Tools seen on their site: {', '.join(lead['tech'][:10])}")
    angle = (lead.get("reasons") or {}).get("angle") if isinstance(lead.get("reasons"), dict) else ""
    if angle:
        lines.append(f"Suggested angle: {angle}")
    lines.append("\n# Signals (most recent first)")
    for s in signals[:3]:
        when = time.strftime("%b %d", time.gmtime(s["occurred_at"]))
        lines.append(f"- [{s['type']}, {when}] {s['title']}\n  {(s.get('text') or '')[:400]}")
    lines += ["\n# Sender", f"Name: {brain.get('sender_name') or '(not set)'}",
              f"Title: {brain.get('sender_title') or ''}", f"Tone: {brain.get('tone')}"]
    if brain.get("calendar_link"):
        lines.append(f"Calendar link (use at most once, in email 2 or 3): {brain['calendar_link']}")
    return "\n".join(lines)


def _first_name(contact: dict | None) -> str:
    name = (contact or {}).get("name") or ""
    return name.split()[0] if name else ""


def _template(lead: dict, contact: dict | None, signals: list[dict], brain: dict) -> dict:
    """No-LLM fallback. Deliberately plain; edit before sending."""
    hi = f"Hi {_first_name(contact)}," if _first_name(contact) else "Hi,"
    co = lead.get("company_name") or "your team"
    s = signals[0] if signals else {"type": "import", "title": ""}
    ex = s.get("extra") or {}
    hook = {
        "hiring": f"I saw {co} is hiring for {ex.get('role') or 'a new role'}.",
        "funding": f"Congratulations on the {ex.get('round') or 'new'} round{(' of ' + ex['amount']) if ex.get('amount') else ''}.",
        "competitor_mention": f"I came across your comment about {ex.get('term') or 'your current tooling'}.",
        "keyword_intent": f"I came across your post about {ex.get('term') or 'this'}.",
        "repo_star": f"I noticed you starred {ex.get('repo') or 'a project in our space'}.",
        "launch": f"I saw the launch of {co}.",
    }.get(s["type"], f"I have been looking at what {co} does.")
    pain = (brain.get("pains") or ["this"])[0].rstrip(".")
    what = brain.get("one_liner") or brain.get("offer") or "we help with exactly that"
    proof = (brain.get("proof_points") or [""])[0]
    subj = f"{co.lower()} + {(brain.get('company') or 'us').lower()}"
    e1 = f"{hi}\n\n{hook} Teams at that stage often tell us {pain[0].lower() + pain[1:]}.\n\n{what}\n\nIs that on your plate right now?"
    e2 = f"{hi}\n\nOne more thought on my note below. {proof or 'Happy to share how others in your position handled this.'}\n\nWorth a short conversation?"
    e3 = f"{hi}\n\nI will close the loop here. If this is not a priority, no reply needed. If it is, I am glad to send over details."
    return {
        "emails": [{"subject": subj, "body": e1}, {"subject": "Re: " + subj, "body": e2},
                   {"subject": "Re: " + subj, "body": e3}],
        "linkedin_note": f"{hi} {hook} I work on this problem at {brain.get('company') or 'my company'} and would like to connect."[:LINKEDIN_NOTE_MAX],
        "linkedin_followup": f"Thanks for connecting. {hook} {what} Is {pain[0].lower() + pain[1:]} something you are dealing with?",
        "call": {
            "opener": f"Hi{(' ' + _first_name(contact)) if _first_name(contact) else ''}, this is {brain.get('sender_name') or '...'} from {brain.get('company') or '...'}. Have I caught you with thirty seconds?",
            "reason": f"{hook} {what}",
            "questions": [f"A lot of teams tell us this: {pain[0].lower() + pain[1:]}. Is that true for you?",
                          "What would have to be true for this to be worth a look this quarter?"],
            "voicemail": f"Hi, it's {brain.get('sender_name') or '...'} from {brain.get('company') or '...'}. {hook} I will send a short email with the details.",
        },
    }


async def draft_sequence(lead_id: int, channels: list[str] | None = None, replace: bool = False) -> dict:
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        return {"error": "no such lead"}
    play = db.one("SELECT config FROM plays WHERE id=?", (lead["play_id"],)) or {"config": {}}
    channels = channels or play["config"].get("channels") or ["email", "linkedin", "phone"]
    contact = db.one("SELECT * FROM contacts WHERE lead_id=? ORDER BY is_primary DESC, id LIMIT 1", (lead_id,))
    signals = db.q("SELECT * FROM signals WHERE lead_id=? ORDER BY occurred_at DESC", (lead_id,))
    brain = brain_mod.get()
    if replace:
        db.x("DELETE FROM messages WHERE lead_id=? AND status IN ('draft','queued')", (lead_id,))
    elif db.one("SELECT 1 FROM messages WHERE lead_id=?", (lead_id,)):
        return {"skipped": "sequence already exists"}

    seq = await llm.ask_json(SEQ_SYSTEM + "\n\n# Company\n" + brain_mod.summary(brain),
                             _context(lead, contact, signals, brain), SEQ_SCHEMA, effort="medium", max_tokens=6000)
    source = "claude"
    if not seq or len(seq.get("emails", [])) < 1:
        seq, source = _template(lead, contact, signals, brain), "template"

    cid = contact["id"] if contact else None
    mode = db.get_setting("send_mode", "copilot")
    made = []
    now = db.now()
    c = contact or {}
    if "email" in channels and c.get("email") and c.get("email_status") != "invalid":
        for i, em in enumerate(seq["emails"][:3], start=1):
            # In auto mode only step 1 to a verified address goes out unreviewed; everything else waits for you.
            auto = mode == "auto" and c.get("email_status") == "valid"
            db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (lead_id, cid, "email", i, em["subject"], em["body"], "queued" if auto else "draft",
                  now + 86400 * EMAIL_STEP_DAYS[i], now))
            made.append(f"email{i}")
    if "linkedin" in channels and (c.get("linkedin_url") or lead.get("linkedin_company_url")):
        from . import linkedin
        # Hands-off: in automatic mode with an account connected, the invite goes out without review,
        # but only to a known profile. A search link still needs a person to pick the right result.
        has_profile = "linkedin.com/in/" in (c.get("linkedin_url") or "")
        li_auto = mode == "auto" and linkedin.enabled() and has_profile and not c.get("do_not_contact")
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)",
             (lead_id, cid, "linkedin", 1, "Connection request", seq["linkedin_note"][:LINKEDIN_NOTE_MAX],
              "queued" if li_auto else "draft", now, now))
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)",
             (lead_id, cid, "linkedin", 2, "After they accept", seq["linkedin_followup"], "draft", now + 86400 * 2, now))
        made += ["linkedin1", "linkedin2"]
    phone_contact = db.one("SELECT * FROM contacts WHERE lead_id=? AND phone IS NOT NULL ORDER BY is_primary DESC, id LIMIT 1", (lead_id,))
    if "phone" in channels and phone_contact and (phone_contact["phone_detail"] or {}).get("callable", True):
        call = seq["call"]
        script = (f"OPENER\n{call['opener']}\n\nWHY I CALLED\n{call['reason']}\n\nQUESTIONS\n" +
                  "\n".join(f"- {q}" for q in call["questions"]) + f"\n\nVOICEMAIL\n{call['voicemail']}")
        db.x("INSERT INTO messages(lead_id,contact_id,channel,step,subject,body,status,scheduled_at,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?)",
             (lead_id, phone_contact["id"], "call", 1, "Call script", script, "draft", now + 86400, now))
        made.append("call")
    return {"created": made, "source": source}


# ---------- email sending ----------

def _footer() -> str:
    lines = ["", "--"]
    b = brain_mod.get()
    sig = ", ".join(x for x in (b.get("sender_name"), b.get("sender_title"), b.get("company")) if x)
    if sig:
        lines.append(sig)
    lines.append("If this is not relevant, reply \"unsubscribe\" and I will not write again.")
    if config.POSTAL_ADDRESS:
        lines.append(config.POSTAL_ADDRESS)
    return "\n".join(lines)


def _smtp_send(msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    with server:
        server.ehlo()
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASS)
        server.send_message(msg)


def sent_today(channel: str = "email") -> int:
    start = time.time() - (time.time() % 86400)
    return db.one("SELECT COUNT(*) n FROM messages WHERE channel=? AND status IN ('sent','done') AND sent_at>=?",
                  (channel, start))["n"]


def _blocked(m: dict, contact: dict | None, lead: dict) -> str:
    if not contact or not contact.get("email"):
        return "contact has no email address"
    if contact["do_not_contact"]:
        return "contact is marked do-not-contact"
    if contact["email_status"] == "invalid":
        return "email address failed validation"
    dom = contact["email"].split("@")[-1]
    if db.one("SELECT 1 FROM suppression WHERE value IN (?,?)", (contact["email"], dom)):
        return "address is on the suppression list"
    if lead["status"] in ("replied", "won", "lost", "rejected"):
        return f"lead is {lead['status']}"
    return ""


async def send_due() -> dict:
    """Send queued emails that are due. Called every minute by the scheduler."""
    if not config.status()["smtp"]:
        return {"sent": 0, "reason": "SMTP not configured"}
    room = config.EMAIL_DAILY_CAP - sent_today("email")
    if room <= 0:
        return {"sent": 0, "reason": "daily cap reached"}
    due = db.q("SELECT * FROM messages WHERE channel='email' AND status='queued' AND scheduled_at<=? "
               "ORDER BY scheduled_at LIMIT ?", (db.now(), min(room, 5)))
    sent = 0
    for m in due:
        contact = db.one("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
        lead = db.one("SELECT * FROM leads WHERE id=?", (m["lead_id"],))
        why = _blocked(m, contact, lead) if lead else "lead was deleted"
        if why:
            db.x("UPDATE messages SET status='skipped', error=? WHERE id=?", (why, m["id"]))
            continue
        if m["step"] > 1 and not db.one("SELECT 1 FROM messages WHERE lead_id=? AND channel='email' AND step=? AND status='sent'",
                                        (m["lead_id"], m["step"] - 1)):
            db.x("UPDATE messages SET status='skipped', error='earlier step was never sent' WHERE id=?", (m["id"],))
            continue
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((config.SMTP_FROM_NAME or brain_mod.get().get("sender_name") or "", config.SMTP_FROM))
        msg["To"] = contact["email"]
        msg["Subject"] = m["subject"] or ""
        msg["Message-ID"] = email.utils.make_msgid(domain=config.SMTP_FROM.split("@")[-1])
        msg["List-Unsubscribe"] = f"<mailto:{config.SMTP_FROM}?subject=unsubscribe>"
        prev = db.one("SELECT outcome FROM messages WHERE lead_id=? AND channel='email' AND status='sent' "
                      "ORDER BY step DESC LIMIT 1", (m["lead_id"],))
        if prev and prev["outcome"]:
            msg["In-Reply-To"] = prev["outcome"]
            msg["References"] = prev["outcome"]
        msg.set_content((m["body"] or "") + "\n" + _footer())
        try:
            await asyncio.to_thread(_smtp_send, msg)
        except Exception as e:
            log.warning("send failed for message %s: %s", m["id"], e)
            db.x("UPDATE messages SET status='failed', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:300], m["id"]))
            continue
        # `outcome` holds the Message-ID so later steps thread under it.
        db.x("UPDATE messages SET status='sent', sent_at=?, outcome=?, error=NULL WHERE id=?",
             (db.now(), msg["Message-ID"], m["id"]))
        db.x("UPDATE leads SET status='contacted', updated_at=? WHERE id=? AND status='approved'", (db.now(), m["lead_id"]))
        sent += 1
        await asyncio.sleep(20 + (m["id"] % 25))  # spread sends out; bursts look like bulk mail
    return {"sent": sent}


def stop_sequence(lead_id: int, reason: str) -> int:
    return db.x("UPDATE messages SET status='skipped', error=? WHERE lead_id=? AND status IN ('draft','queued')",
                (reason, lead_id))


# ---------- replies ----------

REPLY_SCHEMA = llm.obj({"intent": {"type": "string", "enum": ["interested", "question", "not_now", "not_interested",
                                                               "referral", "unsubscribe", "auto_reply"]},
                        "draft": llm.STR})


def _body_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        from . import web
        text = web.strip_html(text)
    # Drop the quoted original.
    text = re.split(r"\n(?:On .{10,80} wrote:|-{2,} ?Original Message|From: .+\nSent: )", text)[0]
    return "\n".join(l for l in text.splitlines() if not l.startswith(">")).strip()[:4000]


def classify(from_addr: str, subject: str, body: str, headers: dict) -> str:
    low_from = from_addr.lower()
    if "mailer-daemon" in low_from or "postmaster" in low_from or re.search(r"undeliver|delivery (status|fail)|returned mail", subject, re.I):
        return "bounce"
    if headers.get("auto-submitted", "no").lower() != "no" or OOO_RE.search(subject) or OOO_RE.search(body[:300]):
        return "auto_reply"
    if UNSUB_RE.search(body[:600]) or UNSUB_RE.search(subject):
        return "unsubscribe"
    return "reply"


async def handle_inbound(from_addr: str, subject: str, body: str, message_id: str, headers: dict | None = None) -> dict:
    """Record one inbound email and apply its consequences. Pure logic, so it is testable without IMAP."""
    headers = headers or {}
    if message_id and db.one("SELECT 1 FROM replies WHERE message_id=?", (message_id,)):
        return {"duplicate": True}
    kind = classify(from_addr, subject, body, headers)
    addr = validate.normalize_email(email.utils.parseaddr(from_addr)[1])
    contact = None
    if kind == "bounce":
        for cand in validate.EMAIL_FIND_ANY.findall(body):
            contact = db.one("SELECT * FROM contacts WHERE email=?", (cand.lower(),))
            if contact:
                break
    else:
        contact = db.one("SELECT * FROM contacts WHERE email=?", (addr,))
        if not contact and addr:
            # A colleague replying from the same company domain still belongs to the lead.
            contact = db.one("SELECT c.* FROM contacts c JOIN leads l ON l.id=c.lead_id WHERE l.company_domain=? "
                             "AND l.status IN ('contacted','approved') LIMIT 1", (addr.split("@")[-1],))
    if not contact:
        return {"ignored": "not from a known contact"}
    lead_id = contact["lead_id"]
    draft = ""
    if kind == "bounce":
        db.x("UPDATE contacts SET email_status='invalid', email_detail=? WHERE id=?",
             (db.j({"status": "invalid", "reason": "bounced", "score": 0, "flags": ["bounced"]}), contact["id"]))
        db.x("UPDATE messages SET status='skipped', error='address bounced' WHERE contact_id=? AND channel='email' "
             "AND status IN ('draft','queued')", (contact["id"],))
    elif kind == "unsubscribe":
        db.x("INSERT OR IGNORE INTO suppression(value,reason,created_at) VALUES(?,?,?)", (contact["email"], "asked to unsubscribe", db.now()))
        db.x("UPDATE contacts SET do_not_contact=1 WHERE id=?", (contact["id"],))
        stop_sequence(lead_id, "unsubscribed")
        db.x("UPDATE leads SET status='lost', updated_at=? WHERE id=?", (db.now(), lead_id))
    elif kind == "reply":
        return await record_reply(lead_id, contact["id"], from_addr, subject, body, message_id)
    db.x("INSERT INTO replies(lead_id,contact_id,from_addr,subject,body,received_at,kind,draft_reply,message_id) "
         "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, contact["id"], from_addr, subject, body, db.now(), kind, draft, message_id or None))
    return {"kind": kind, "lead_id": lead_id}


async def record_reply(lead_id: int, contact_id: int | None, from_addr: str, subject: str, body: str, message_id: str) -> dict:
    """A human replied on any channel: stop the sequence, mark the lead, draft an answer."""
    if message_id and db.one("SELECT 1 FROM replies WHERE message_id=?", (message_id,)):
        return {"duplicate": True}
    contact = db.one("SELECT * FROM contacts WHERE id=?", (contact_id,)) or {}
    addr = contact.get("email") or from_addr
    kind, draft = "reply", ""
    if True:
        stop_sequence(lead_id, "prospect replied")
        db.x("UPDATE leads SET status='replied', updated_at=? WHERE id=?", (db.now(), lead_id))
        lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        brain = brain_mod.get()
        sent = db.q("SELECT subject, body FROM messages WHERE lead_id=? AND status IN ('sent','done') AND channel!='call' "
                    "ORDER BY sent_at", (lead_id,))
        data = await llm.ask_json(
            "You help a founder answer replies to their outbound email. Classify the reply's intent, then draft the "
            "founder's answer: short, plain, human, directly responsive to what was asked, with one clear next step. "
            "If they are not interested, the draft is one gracious line. Never invent pricing, features or facts not in "
            "the company profile; where a fact is needed and missing, write a bracketed placeholder for the founder to "
            "fill in.\n\n# Company\n" + brain_mod.summary(brain) +
            (f"\nCalendar link: {brain['calendar_link']}" if brain.get("calendar_link") else ""),
            "# What we sent\n" + "\n---\n".join(f"{m['subject']}\n{m['body']}" for m in sent[-2:]) +
            f"\n\n# Their reply (from {contact.get('name') or addr} at {lead.get('company_name')})\n{body}",
            REPLY_SCHEMA, effort="medium", max_tokens=3000)
        if data:
            draft = data["draft"]
            if data["intent"] == "unsubscribe":
                if contact.get("email"):
                    db.x("INSERT OR IGNORE INTO suppression(value,reason,created_at) VALUES(?,?,?)", (contact["email"], "asked to stop", db.now()))
                if contact.get("id"):
                    db.x("UPDATE contacts SET do_not_contact=1 WHERE id=?", (contact["id"],))
            kind = "reply:" + data["intent"]
    db.x("INSERT INTO replies(lead_id,contact_id,from_addr,subject,body,received_at,kind,draft_reply,message_id) "
         "VALUES(?,?,?,?,?,?,?,?,?)", (lead_id, contact.get("id"), from_addr, subject, body, db.now(), kind, draft, message_id or None))
    return {"kind": kind, "lead_id": lead_id}


def _imap_fetch(since_days: int = 3) -> list[tuple[str, str, str, str, dict]]:
    out = []
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30) as im:
        im.login(config.IMAP_USER, config.IMAP_PASS)
        im.select("INBOX", readonly=True)
        since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 86400 * since_days))
        typ, data = im.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            return out
        for num in data[0].split()[-200:]:
            typ, parts = im.fetch(num, "(RFC822)")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
            out.append((str(msg.get("From", "")), str(msg.get("Subject", "")), _body_of(msg),
                        str(msg.get("Message-ID", "")).strip(),
                        {"auto-submitted": str(msg.get("Auto-Submitted", "no"))}))
    return out


async def poll_replies() -> dict:
    if not config.status()["imap"]:
        return {"checked": 0, "reason": "IMAP not configured"}
    known = {r["message_id"] for r in db.q("SELECT message_id FROM replies WHERE message_id IS NOT NULL")}
    try:
        mails = await asyncio.to_thread(_imap_fetch)
    except Exception as e:
        log.warning("IMAP poll failed: %s", e)
        return {"checked": 0, "error": f"{type(e).__name__}: {e}"[:200]}
    handled = 0
    for frm, subj, body, mid, hdrs in mails:
        if mid in known:
            continue
        res = await handle_inbound(frm, subj, body, mid, hdrs)
        handled += 1 if res.get("kind") else 0
    return {"checked": len(mails), "handled": handled}
