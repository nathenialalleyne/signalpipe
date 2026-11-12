"""LinkedIn through a session provider (Unipile's v1 API), the same mechanism hosted tools use.

Off unless UNIPILE_DSN and UNIPILE_API_KEY are set and a LinkedIn account is connected (through the
hosted login, or UNIPILE_ACCOUNT_ID). When off, LinkedIn steps stay in the click-to-send queue.

What it does when on:
- Signals: people who reacted to or commented on the pages and profiles in a play's `linkedin_watch`
  list, and authors of recent posts matching the play's keywords.
- Outreach: sends approved connection requests, notices acceptances, sends the follow-up, and treats an
  inbound message as a reply that stops the sequence.

This acts as your LinkedIn account, which LinkedIn's terms do not permit for third-party tools. The caps
and pacing below are deliberately well under the provider's own guidance to keep the account safe.
"""
import asyncio
import datetime as dt
import logging
import random
import re
import time
import zoneinfo

from . import config, db, web
from .collectors import Signal, cutoff, terms

log = logging.getLogger("signalpipe.linkedin")

DSN = config.env("UNIPILE_DSN")              # e.g. api8.unipile.com:13851
API_KEY = config.env("UNIPILE_API_KEY")
WEEKLY_CAP = int(config.env("LINKEDIN_WEEKLY_CAP", "80") or 80)
NOTE_MAX = int(config.env("LINKEDIN_NOTE_MAX", "200") or 200)   # 300 on paid LinkedIn accounts
TZ = config.env("LINKEDIN_TZ", "America/New_York")
READ_BUDGET = int(config.env("LINKEDIN_READS_PER_RUN", "30") or 30)  # provider guidance is ~100 reads a day

STOP_FOR_TODAY = {"cannot_resend_yet", "limit_exceeded", "connection_limit_reached", "too_many_requests"}
ACCOUNT_BROKEN = {"disconnected_account", "expired_credentials", "account_restricted", "invalid_credentials",
                  "missing_credentials", "checkpoint_error", "action_required", "password_reset_required"}


class LinkedInError(Exception):
    def __init__(self, status: int, kind: str, detail: str = ""):
        super().__init__(f"{status} {kind} {detail}".strip())
        self.status, self.kind = status, kind


def account_id() -> str:
    """The connected LinkedIn account: UNIPILE_ACCOUNT_ID, or the one stored by the hosted login."""
    return config.env("UNIPILE_ACCOUNT_ID") or db.get_setting("linkedin_account_id", "")


def provider_ready() -> bool:
    return bool(DSN and API_KEY)


def enabled() -> bool:
    return bool(provider_ready() and account_id())


async def connect_url(return_to: str) -> str:
    """A one-hour link to the provider's hosted login page. You sign in to LinkedIn there, not here."""
    if not provider_ready():
        raise LinkedInError(400, "not_configured", "Set UNIPILE_DSN and UNIPILE_API_KEY first")
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {"type": "create", "providers": ["LINKEDIN"], "api_url": f"https://{DSN}", "expiresOn": expires,
            "name": "signalpipe"}
    if return_to.startswith("http"):
        body["success_redirect_url"] = return_to + "#/settings?linkedin=connected"
        body["failure_redirect_url"] = return_to + "#/settings?linkedin=failed"
    res = await _req("POST", "/hosted/accounts/link", json=body)
    return res["url"]


async def refresh_account() -> dict:
    """After the hosted login: find the LinkedIn account on the provider and remember it."""
    if not provider_ready():
        return {"connected": False, "reason": "provider keys are not set"}
    data = await _req("GET", "/accounts")
    li = [a for a in data.get("items", []) if str(a.get("type", "")).upper() == "LINKEDIN"]
    if not li:
        db.set_setting("linkedin_account_id", "")
        return {"connected": False, "reason": "no LinkedIn account is connected yet"}
    acct = li[-1]
    db.set_setting("linkedin_account_id", acct["id"])
    db.set_setting("linkedin_account_name", acct.get("name", ""))
    db.set_setting("linkedin_error", "")
    return {"connected": True, "name": acct.get("name", "")}


def disconnect() -> None:
    """Stop using the account here. (Remove it in the provider dashboard to end the session itself.)"""
    db.set_setting("linkedin_account_id", "")


def state() -> dict:
    return {"enabled": enabled(), "provider_ready": provider_ready(),
            "account": db.get_setting("linkedin_account_name", "") if account_id() else "", "error": db.get_setting("linkedin_error", ""),
            "paused_until": db.get_setting("linkedin_pause_until", 0), "weekly_cap": WEEKLY_CAP, "note_max": NOTE_MAX,
            "sent_this_week": _invites_since(7 * 86400)}


async def _req(method: str, path: str, params: dict | None = None, json: dict | None = None, files: dict | None = None):
    url = f"https://{DSN}/api/v1{path}"
    headers = {"X-API-KEY": API_KEY, "accept": "application/json"}
    r = await web.client().request(method, url, params=params, json=json, files=files, headers=headers, timeout=40)
    if r.status_code >= 400:
        kind = ""
        try:
            kind = str(r.json().get("type", "")).split("/")[-1]
        except Exception:
            pass
        raise LinkedInError(r.status_code, kind or "error", r.text[:160])
    return r.json()


async def _pause() -> None:
    await asyncio.sleep(random.uniform(2.0, 6.0))  # never fire calls at a fixed rhythm


def _slug(url: str) -> tuple[str, str]:
    """('company'|'user', identifier) from a LinkedIn URL or bare slug."""
    m = re.search(r"linkedin\.com/(company|school|in)/([^/?#]+)", url or "", re.I)
    if m:
        return ("user" if m.group(1).lower() == "in" else "company"), m.group(2)
    return "company", (url or "").strip().strip("/")


async def _entity_id(kind: str, ident: str) -> str:
    """Provider-internal id for a company page or member, cached for 90 days."""
    key = f"li:id:{kind}:{ident.lower()}"
    cached = db.cache_get(key, 90 * 86400)
    if cached:
        return cached
    if kind == "company":
        data = await _req("GET", f"/linkedin/company/{ident}", {"account_id": account_id()})
        eid = str(data.get("id") or "")
    else:
        data = await _req("GET", f"/users/{ident}", {"account_id": account_id()})
        eid = str(data.get("provider_id") or "")
    if eid:
        db.cache_set(key, eid)
    return eid


def company_from_headline(headline: str) -> str:
    """'VP Sales at Acme | ex-Foo' -> 'Acme'. Empty when the headline does not name an employer."""
    m = re.search(r"(?:\bat\b|@)\s+([A-Z0-9][\w&.'\- ]{1,40}?)(?=\s*(?:[|•·,/()\-–]|$))", headline or "")
    return m.group(1).strip() if m else ""


def _person_signal(kind: str, author: dict, title: str, text: str, url: str, ts: float, extra: dict) -> Signal | None:
    if not author or author.get("type") == "COMPANY" or author.get("is_company") or not author.get("name"):
        return None
    headline = author.get("headline") or ""
    pid = str(author.get("id") or "")
    prof = author.get("profile_url") or (f"https://www.linkedin.com/in/{author['public_identifier']}" if author.get("public_identifier") else "")
    return Signal(
        source="linkedin", type=kind, title=title[:300], text=text[:1200], url=f"{url}#{pid or author['name']}",
        occurred_at=ts, company_name=company_from_headline(headline), person_name=author["name"],
        person_title=headline[:160], person_handle=f"li:{pid or prof}", linkedin_url=prof.split("?")[0],
        extra={**extra, "provider_id": pid, "post_url": url, "network_distance": author.get("network_distance")},
    )


def _ts(post: dict) -> float:
    try:
        return dt.datetime.fromisoformat(str(post.get("parsed_datetime")).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


async def collect(cfg: dict) -> list[Signal]:
    """Engagers on watched pages and authors of keyword posts. Read-only, bounded by READ_BUDGET."""
    if not enabled():
        return []
    since, budget, out = cutoff(cfg), READ_BUDGET, []
    try:
        for entry in terms(cfg, "linkedin_watch")[:8]:
            if budget <= 3:
                break
            kind, ident = _slug(entry)
            eid = await _entity_id(kind, ident)
            budget -= 1
            if not eid:
                continue
            await _pause()
            posts = await _req("GET", f"/users/{eid}/posts",
                               {"account_id": account_id(), "limit": 6, **({"is_company": "true"} if kind == "company" else {})})
            budget -= 1
            fresh = [p for p in posts.get("items", []) if _ts(p) >= since and not p.get("is_repost")]
            fresh.sort(key=lambda p: -(p.get("comment_counter", 0) * 3 + p.get("reaction_counter", 0)))
            for p in fresh[:3]:
                if budget <= 1:
                    break
                sid, url, ts = p.get("social_id") or p.get("id"), p.get("share_url") or entry, _ts(p)
                snippet = (p.get("text") or "")[:200]
                await _pause()
                comments = await _req("GET", f"/posts/{sid}/comments", {"account_id": account_id(), "limit": 50})
                budget -= 1
                for c in comments.get("items", []):
                    det = c.get("author_details") or {}
                    author = {"name": c.get("author"), "id": det.get("id"), "headline": det.get("headline"),
                              "profile_url": det.get("profile_url"), "network_distance": det.get("network_distance")}
                    s = _person_signal("competitor_engagement", author, f"{c.get('author')} commented on {ident}'s post",
                                       f"Comment: {c.get('text') or ''}\nPost: {snippet}", url, ts,
                                       {"watched": ident, "action": "comment", "asks": True})
                    if s:
                        out.append(s)
                await _pause()
                reactions = await _req("GET", f"/posts/{sid}/reactions", {"account_id": account_id(), "limit": 100})
                budget -= 1
                for r in reactions.get("items", []):
                    a = r.get("author") or {}
                    s = _person_signal("competitor_engagement", a, f"{a.get('name')} reacted to {ident}'s post",
                                       f"Post: {snippet}", url, ts, {"watched": ident, "action": "reaction"})
                    if s:
                        out.append(s)
        for kw in terms(cfg, "keywords")[:5]:
            if budget <= 0:
                break
            await _pause()
            res = await _req("POST", "/linkedin/search", {"account_id": account_id()},
                             json={"api": "classic", "category": "posts", "keywords": kw, "date_posted": "past_week", "sort_by": "date"})
            budget -= 1
            for p in res.get("items", [])[:25]:
                a = p.get("author") or {}
                s = _person_signal("keyword_post", a, f"{a.get('name')} posted about {kw}", p.get("text") or "",
                                   p.get("share_url") or "", _ts(p), {"term": kw})
                if s and _ts(p) >= since:
                    out.append(s)
        db.set_setting("linkedin_error", "")
    except LinkedInError as e:
        log.warning("LinkedIn collect stopped: %s", e)
        if e.kind in ACCOUNT_BROKEN:
            db.set_setting("linkedin_error", f"LinkedIn account needs attention: {e.kind}")
    return out


# ---------- outreach ----------

def _invites_since(seconds: float) -> int:
    return db.one("SELECT COUNT(*) n FROM messages WHERE channel='linkedin' AND step=1 AND status='done' AND sent_at>=?",
                  (time.time() - seconds,))["n"]


def _working_hours(now: dt.datetime | None = None) -> bool:
    try:
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(zoneinfo.ZoneInfo(TZ))
    except Exception:
        local = now or dt.datetime.now()
    return local.weekday() < 5 and 9 <= local.hour < 18


async def _provider_id(contact: dict) -> str:
    pid = (contact.get("email_detail") or {}).get("li_provider_id") or ""
    if pid:
        return pid
    sig = db.one("SELECT extra FROM signals WHERE lead_id=? AND source='linkedin' AND json_extract(extra,'$.provider_id')!='' "
                 "ORDER BY occurred_at DESC LIMIT 1", (contact["lead_id"],))
    if sig and contact.get("linkedin_url") and sig["extra"].get("provider_id"):
        owner = db.one("SELECT 1 FROM signals WHERE lead_id=? AND source='linkedin' AND url LIKE ?",
                       (contact["lead_id"], f"%#{sig['extra']['provider_id']}"))
        if owner:
            pid = sig["extra"]["provider_id"]
    if not pid:
        kind, ident = _slug(contact.get("linkedin_url") or "")
        if kind != "user" or "/search/" in (contact.get("linkedin_url") or ""):
            return ""  # a search link, not a profile: only a person can pick the right result
        pid = await _entity_id("user", ident)
    if pid:
        detail = {**(contact.get("email_detail") or {}), "li_provider_id": pid}
        db.x("UPDATE contacts SET email_detail=? WHERE id=?", (db.j(detail), contact["id"]))
    return pid


async def send_next() -> dict:
    """Send at most one queued LinkedIn step. Called every few minutes; pacing comes from the skips."""
    if not enabled():
        return {"sent": 0, "reason": "provider not configured"}
    if db.get_setting("linkedin_pause_until", 0) > time.time():
        return {"sent": 0, "reason": "paused after hitting a LinkedIn limit"}
    if not _working_hours():
        return {"sent": 0, "reason": "outside working hours"}
    if random.random() < 0.5:
        return {"sent": 0, "reason": "pacing"}
    m = db.one("SELECT * FROM messages WHERE channel='linkedin' AND status='queued' AND scheduled_at<=? "
               "ORDER BY step DESC, scheduled_at LIMIT 1", (db.now(),))
    if not m:
        return {"sent": 0}
    if m["step"] == 1 and (_invites_since(86400) >= config.LINKEDIN_DAILY_CAP or _invites_since(7 * 86400) >= WEEKLY_CAP):
        return {"sent": 0, "reason": "invite cap reached"}
    contact = db.one("SELECT * FROM contacts WHERE id=?", (m["contact_id"],))
    lead = db.one("SELECT * FROM leads WHERE id=?", (m["lead_id"],))
    if not contact or not lead or contact["do_not_contact"] or lead["status"] in ("replied", "won", "lost", "rejected"):
        db.x("UPDATE messages SET status='skipped', error='lead is closed or contact is do-not-contact' WHERE id=?", (m["id"],))
        return {"sent": 0}
    try:
        pid = await _provider_id(contact)
        if not pid:
            db.x("UPDATE messages SET status='draft', error='No profile URL for this contact; open LinkedIn and send it by hand' WHERE id=?", (m["id"],))
            return {"sent": 0}
        if m["step"] == 1:
            res = await _req("POST", "/users/invite", json={"account_id": account_id(), "provider_id": pid,
                                                              "message": (m["body"] or "")[:NOTE_MAX]})
            db.x("UPDATE messages SET status='done', sent_at=?, outcome=?, error=NULL WHERE id=?",
                 (db.now(), "invited:" + str(res.get("invitation_id", "")), m["id"]))
        else:
            res = await _req("POST", "/chats", files={"account_id": (None, account_id()), "attendees_ids": (None, pid),
                                                       "text": (None, m["body"] or "")})
            db.x("UPDATE messages SET status='done', sent_at=?, outcome=?, error=NULL WHERE id=?",
                 (db.now(), "chat:" + str(res.get("chat_id", "")), m["id"]))
        db.x("UPDATE leads SET status='contacted', updated_at=? WHERE id=? AND status='approved'", (db.now(), lead["id"]))
        db.set_setting("linkedin_error", "")
        return {"sent": 1, "step": m["step"]}
    except LinkedInError as e:
        if e.kind in ("already_connected", "action_already_performed") and m["step"] == 1:
            db.x("UPDATE messages SET status='done', sent_at=?, outcome='accepted' WHERE id=?", (db.now(), m["id"]))
            _release_followup(m["lead_id"])
            return {"sent": 0, "reason": "already connected"}
        if e.kind in STOP_FOR_TODAY or e.status == 429:
            db.set_setting("linkedin_pause_until", time.time() + 20 * 3600)
            return {"sent": 0, "reason": f"LinkedIn limit ({e.kind}); paused for 20 hours"}
        if e.kind in ACCOUNT_BROKEN:
            db.set_setting("linkedin_error", f"LinkedIn account needs attention: {e.kind}")
            db.set_setting("linkedin_pause_until", time.time() + 6 * 3600)
            return {"sent": 0, "reason": e.kind}
        db.x("UPDATE messages SET status='failed', error=? WHERE id=?", (str(e)[:300], m["id"]))
        return {"sent": 0, "error": str(e)[:200]}


def _release_followup(lead_id: int) -> None:
    """Once connected, the follow-up becomes sendable: queued in auto mode, otherwise left for your review."""
    auto = db.get_setting("send_mode", "copilot") == "auto"
    db.x("UPDATE messages SET status=?, scheduled_at=?, error=NULL WHERE lead_id=? AND channel='linkedin' AND step=2 "
         "AND status IN ('draft','queued')",
         ("queued" if auto else "draft", db.now() + random.uniform(1, 6) * 3600, lead_id))


async def sync() -> dict:
    """Notice accepted invitations and inbound messages. Called about hourly."""
    if not enabled():
        return {}
    accepted = replies = 0
    try:
        waiting = db.q("SELECT m.id, m.lead_id, c.linkedin_url, c.email_detail FROM messages m JOIN contacts c ON c.id=m.contact_id "
                       "WHERE m.channel='linkedin' AND m.step=1 AND m.status='done' AND m.outcome LIKE 'invited:%'")
        if waiting:
            rel = await _req("GET", "/users/relations", {"account_id": account_id(), "limit": 200})
            ids, slugs = set(), set()
            for r in rel.get("items", []):
                ids.add(str(r.get("member_id")))
                slugs.add(str(r.get("public_identifier") or "").lower())
            for w in waiting:
                pid = (w["email_detail"] or {}).get("li_provider_id")
                slug = _slug(w["linkedin_url"] or "")[1].lower()
                if (pid and pid in ids) or (slug and slug in slugs):
                    db.x("UPDATE messages SET outcome='accepted' WHERE id=?", (w["id"],))
                    _release_followup(w["lead_id"])
                    accepted += 1
        last = db.get_setting("linkedin_last_msg_sync", time.time() - 3 * 86400)
        after = dt.datetime.fromtimestamp(last, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        await _pause()
        msgs = await _req("GET", "/messages", {"account_id": account_id(), "after": after, "limit": 100})
        from . import outreach
        for msg in msgs.get("items", []):
            if msg.get("is_sender") or not msg.get("text"):
                continue
            sent = db.one("SELECT lead_id, contact_id FROM messages WHERE channel='linkedin' AND outcome=?", ("chat:" + str(msg.get("chat_id")),))
            if not sent:
                c = db.one("SELECT lead_id, id contact_id FROM contacts WHERE json_extract(email_detail,'$.li_provider_id')=?",
                           (str(msg.get("sender_id")),))
                sent = c if c and db.one("SELECT 1 FROM messages WHERE lead_id=? AND channel='linkedin' AND status='done'", (c["lead_id"],)) else None
            if sent:
                res = await outreach.record_reply(sent["lead_id"], sent["contact_id"], "LinkedIn", "LinkedIn message",
                                                  msg["text"], f"li:{msg.get('id')}")
                replies += 1 if res.get("kind") else 0
        db.set_setting("linkedin_last_msg_sync", time.time())
        db.set_setting("linkedin_error", "")
    except LinkedInError as e:
        log.warning("LinkedIn sync failed: %s", e)
        if e.kind in ACCOUNT_BROKEN:
            db.set_setting("linkedin_error", f"LinkedIn account needs attention: {e.kind}")
        return {"error": str(e)[:200]}
    return {"accepted": accepted, "replies": replies}
