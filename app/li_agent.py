"""Server side of the local LinkedIn agent.

The agent (agent/linkedin-agent.mjs) runs on your own computer and drives your own Chrome, signed in by
you. It asks this module for one unit of work at a time and reports back. All pacing, caps and
bookkeeping live here, so the agent stays a thin pair of hands.

Work kinds handed out: invite, message, check_connections, check_replies, collect, idle.
"""
import hashlib
import random
import time

from . import config, db, linkedin, outreach, pipeline, scoring
from .collectors import terms

def _minutes(name: str, default: float) -> float:
    try:
        return float(config.env(name, str(default))) * 60
    except ValueError:
        return default * 60


ACTIVE_WINDOW = 3 * 86400                                # connected if the agent checked in within three days
CHECK_EVERY = _minutes("LINKEDIN_CHECK_MIN", 55)         # look for acceptances and replies about hourly
COLLECT_EVERY = _minutes("LINKEDIN_COLLECT_MIN", 720)    # read watched pages twice a day
MIN_GAP = _minutes("LINKEDIN_MIN_GAP_MIN", 8)            # spacing between two sends
MAX_GAP = max(MIN_GAP, _minutes("LINKEDIN_MAX_GAP_MIN", 20))


def active() -> bool:
    return time.time() - db.get_setting("li_agent_seen", 0) < ACTIVE_WINDOW


SCREEN_URL = "/screen/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=screen/websockify"
LEASE = 15 * 60  # a step handed to an agent is held this long before another agent may take it


def on_server() -> bool:
    """True when the agent runs in a container next to the app, with its window reachable from the dashboard."""
    return config.env("AGENT_SCREEN") == "1"


def status() -> dict:
    seen = db.get_setting("li_agent_seen", 0)
    return {"active": active(), "last_seen": seen, "online": time.time() - seen < 20 * 60,
            "blocked": db.get_setting("li_agent_blocked", ""), "on_server": on_server(),
            "screen_url": SCREEN_URL if on_server() else ""}


def _release_leases() -> None:
    db.x("UPDATE messages SET status='queued', scheduled_at=? WHERE channel='linkedin' AND status='sending'", (time.time(),))


def _idle(seconds: float, reason: str) -> dict:
    return {"kind": "idle", "seconds": int(seconds), "reason": reason}


def _profile(contact: dict | None) -> str:
    url = (contact or {}).get("linkedin_url") or ""
    return url.split("?")[0] if "linkedin.com/in/" in url else ""


def next_work() -> dict:
    """One thing for the agent to do now, or how long to wait."""
    now = time.time()
    db.set_setting("li_agent_seen", now)
    if linkedin.unipile_enabled():
        return _idle(3600, "a LinkedIn provider is connected and handles sending")
    if db.get_setting("li_agent_blocked", ""):
        return _idle(900, "waiting for you to sign in again: " + db.get_setting("li_agent_blocked", ""))
    paused = db.get_setting("linkedin_pause_until", 0)
    if paused > now:
        return _idle(min(paused - now, 1800), "paused after LinkedIn signalled a limit")

    # 1. A send that is due, inside working hours, under the caps, and not too soon after the last one.
    if linkedin._working_hours() and db.get_setting("li_agent_next_send_at", 0) <= now:
        # 'sending' with a past scheduled_at is a lease that ran out (the agent holding it never answered).
        for m in db.q("SELECT * FROM messages WHERE channel='linkedin' AND status IN ('queued','sending') "
                      "AND scheduled_at<=? ORDER BY step DESC, scheduled_at LIMIT 8", (now,)):
            contact = db.one("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
            lead = db.one("SELECT * FROM leads WHERE id=?", (m["lead_id"],))
            if not contact or not lead or contact["do_not_contact"] or lead["status"] in ("replied", "won", "lost", "rejected"):
                db.x("UPDATE messages SET status='skipped', error='lead is closed or contact is do-not-contact' WHERE id=?", (m["id"],))
                continue
            url = _profile(contact)
            if not url:
                db.x("UPDATE messages SET status='draft', error='No profile URL for this contact; open LinkedIn and send it by hand' WHERE id=?", (m["id"],))
                continue
            if m["step"] == 1:
                if linkedin._invites_since(86400) >= config.LINKEDIN_DAILY_CAP or linkedin._invites_since(7 * 86400) >= linkedin.WEEKLY_CAP:
                    continue  # invites are capped; a follow-up to an existing connection may still go
                db.x("UPDATE messages SET status='sending', scheduled_at=? WHERE id=?", (now + LEASE, m["id"]))
                return {"kind": "invite", "message_id": m["id"], "profile_url": url, "name": contact["name"] or "",
                        "note": (m["body"] or "")[: linkedin.NOTE_MAX]}
            db.x("UPDATE messages SET status='sending', scheduled_at=? WHERE id=?", (now + LEASE, m["id"]))
            return {"kind": "message", "message_id": m["id"], "profile_url": url, "name": contact["name"] or "",
                    "text": m["body"] or ""}

    # 2. Hourly: did anyone accept, did anyone reply.
    if now - db.get_setting("li_agent_checked_at", 0) > CHECK_EVERY:
        pending = db.q("SELECT m.id message_id, c.linkedin_url FROM messages m JOIN contacts c ON c.id=m.contact_id "
                       "WHERE m.channel='linkedin' AND m.step=1 AND m.status='done' AND m.outcome LIKE 'invited:%' "
                       "AND m.sent_at>? LIMIT 200", (now - 45 * 86400,))
        pending = [{"message_id": p["message_id"], "slug": linkedin._slug(p["linkedin_url"])[1].lower()}
                   for p in pending if _profile(p)]
        if pending and not db.get_setting("li_agent_did_connections", False):
            db.set_setting("li_agent_did_connections", True)
            return {"kind": "check_connections", "pending": pending}
        threads = db.q("SELECT m.id message_id, m.lead_id, m.contact_id, m.body, c.linkedin_url, c.name FROM messages m "
                       "JOIN contacts c ON c.id=m.contact_id JOIN leads l ON l.id=m.lead_id "
                       "WHERE m.channel='linkedin' AND m.step=2 AND m.status='done' AND l.status='contacted' "
                       "AND m.sent_at>? ORDER BY m.sent_at DESC LIMIT 8", (now - 21 * 86400,))
        threads = [{"message_id": t["message_id"], "lead_id": t["lead_id"], "contact_id": t["contact_id"],
                    "profile_url": _profile(t), "name": t["name"] or "", "sent_text": (t["body"] or "")[:60]}
                   for t in threads if _profile(t)]
        db.set_setting("li_agent_checked_at", now)
        db.set_setting("li_agent_did_connections", False)
        if threads:
            return {"kind": "check_replies", "threads": threads}

    # 3. Twice a day: read the pages each play watches.
    for play in db.q("SELECT * FROM plays WHERE status='active'"):
        cfg = play["config"]
        if "linkedin" not in (cfg.get("sources") or []):
            continue
        pages, kws = terms(cfg, "linkedin_watch")[:6], terms(cfg, "keywords")[:3]
        key = f"li_agent_collected:{play['id']}"
        if (pages or kws) and now - db.get_setting(key, 0) > COLLECT_EVERY:
            db.set_setting(key, now)
            return {"kind": "collect", "play_id": play["id"], "pages": pages, "keywords": kws}

    return _idle(random.uniform(240, 540), "nothing due")


async def apply_result(r: dict) -> dict:
    """Record what the agent did. Every outcome maps to an explicit state; nothing is assumed to have worked."""
    kind, now = r.get("kind"), time.time()
    db.set_setting("li_agent_seen", now)
    if kind == "ready":  # the agent confirmed it is signed in
        db.set_setting("li_agent_blocked", "")
        db.set_setting("linkedin_error", "")
        return {"ok": True}
    if kind == "blocked":  # LinkedIn showed a sign-in or verification page: stop and wait for the person
        why = (r.get("reason") or "LinkedIn asked you to sign in or verify")[:200]
        db.set_setting("li_agent_blocked", why)
        if on_server():
            db.set_setting("linkedin_error", f"LinkedIn agent is waiting for you: {why}. Open Settings, "
                                             "then \"Open the agent's screen\", and sign in there.")
        else:
            db.set_setting("linkedin_error", f"LinkedIn agent stopped: {why}. Run the agent's login command.")
        _release_leases()
        return {"ok": True}

    if kind in ("invite", "message"):
        m = db.one("SELECT * FROM messages WHERE id=? AND channel='linkedin'", (r.get("message_id"),))
        if not m:
            return {"ok": False, "error": "no such message"}
        out = r.get("outcome", "")
        gap = now + random.uniform(MIN_GAP, MAX_GAP)
        if out in ("sent", "sent_without_note", "already_pending", "unverified"):
            tag = ("invited:agent" if kind == "invite" else "chat:agent") + ("" if out == "sent" else f":{out}")
            db.x("UPDATE messages SET status='done', sent_at=?, outcome=?, error=NULL WHERE id=?", (now, tag, m["id"]))
            db.x("UPDATE leads SET status='contacted', updated_at=? WHERE id=? AND status='approved'", (now, m["lead_id"]))
            db.set_setting("li_agent_next_send_at", gap)
        elif out == "already_connected" and kind == "invite":
            db.x("UPDATE messages SET status='done', sent_at=?, outcome='accepted' WHERE id=?", (now, m["id"]))
            linkedin._release_followup(m["lead_id"])
        elif out == "limit":
            db.set_setting("linkedin_pause_until", now + 20 * 3600)
            db.x("UPDATE messages SET status='queued', scheduled_at=? WHERE id=?", (now, m["id"]))
        elif out == "not_connected":
            db.x("UPDATE messages SET status='draft', error='Not connected to this person yet' WHERE id=?", (m["id"],))
        else:
            db.x("UPDATE messages SET status='failed', error=? WHERE id=?",
                 ((f"{out}: " + str(r.get("detail") or ""))[:300], m["id"]))
            db.set_setting("li_agent_next_send_at", gap)
        return {"ok": True}

    if kind == "check_connections":
        n = 0
        for mid in r.get("accepted") or []:
            m = db.one("SELECT * FROM messages WHERE id=? AND channel='linkedin' AND step=1", (mid,))
            if m and str(m["outcome"]).startswith("invited:"):
                db.x("UPDATE messages SET outcome='accepted' WHERE id=?", (mid,))
                linkedin._release_followup(m["lead_id"])
                n += 1
        return {"ok": True, "accepted": n}

    if kind == "check_replies":
        n = 0
        for rep in r.get("replies") or []:
            text = (rep.get("text") or "").strip()
            if not text:
                continue
            res = await outreach.record_reply(int(rep["lead_id"]), rep.get("contact_id"), "LinkedIn", "LinkedIn message",
                                              text[:4000], f"li-agent:{rep['message_id']}:{hashlib.sha1(text.encode()).hexdigest()[:12]}")
            n += 1 if res.get("kind") else 0
        return {"ok": True, "replies": n}

    if kind == "collect":
        play = db.one("SELECT id FROM plays WHERE id=?", (r.get("play_id"),))
        if not play:
            return {"ok": False, "error": "no such play"}
        sigs = []
        for p in (r.get("people") or [])[:400]:
            action = p.get("action") or "reaction"
            watched, post = (p.get("watched") or "")[:80], (p.get("post_text") or "")[:300]
            author = {"name": (p.get("name") or "").strip(), "headline": p.get("headline") or "",
                      "profile_url": p.get("profile_url") or "", "id": linkedin._slug(p.get("profile_url") or "")[1]}
            if action == "post":
                s = linkedin._person_signal("keyword_post", author, f"{author['name']} posted about {p.get('term', '')}",
                                            post, p.get("post_url") or "", now, {"term": p.get("term", "")})
            else:
                title = f"{author['name']} {'commented on' if action == 'comment' else 'reacted to'} {watched}'s post"
                body = (f"Comment: {p.get('comment', '')}\n" if p.get("comment") else "") + f"Post: {post}"
                s = linkedin._person_signal("competitor_engagement", author, title, body, p.get("post_url") or watched, now,
                                            {"watched": watched, "action": action, "asks": action == "comment"})
            if s:
                s.extra["provider_id"] = ""  # the agent works from profile URLs, not provider ids
                sigs.append(s)
        new_sig, new_leads, touched = pipeline.ingest(play["id"], sigs)
        for lid in touched:
            from . import enrich
            enrich.set_primary(lid)
        await scoring.score_leads(list(touched))
        return {"ok": True, "new_signals": new_sig, "new_leads": new_leads}

    return {"ok": False, "error": f"unknown kind {kind!r}"}
