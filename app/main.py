"""HTTP API, static UI and the background scheduler."""
import asyncio
import contextlib
import csv
import hashlib
import hmac
import io
import logging
import os
import time
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import brain as brain_mod
from . import config, db, enrich, insights, li_agent, linkedin, llm, outreach, pipeline, scoring, seo, validate, web
from .collectors import SOURCE_INFO

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("signalpipe")
STATIC = os.path.join(os.path.dirname(__file__), "static")
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def scheduler() -> None:
    """One loop, three cadences: send mail every minute, read replies every 10, run plays every N hours."""
    tick = 0
    while True:
        try:
            await asyncio.sleep(60)
            tick += 1
            await outreach.send_due()
            if tick % 4 == 0:
                await linkedin.send_next()
            if tick % 10 == 0:
                await outreach.poll_replies()
            if tick % 45 == 0:
                await linkedin.sync()
            if tick % 15 == 0:
                due = db.q("SELECT id FROM plays WHERE status='active' AND (last_run_at IS NULL OR last_run_at < ?)",
                           (db.now() - config.RUN_INTERVAL_HOURS * 3600,))
                for p in due:
                    await pipeline.run_play(p["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    task = asyncio.create_task(scheduler()) if config.env("SIGNALPIPE_SCHEDULER", "1") != "0" else None
    if not config.APP_PASSWORD:
        log.warning("APP_PASSWORD is not set: the app is open to anyone who can reach it")
    yield
    if task:
        task.cancel()
    await web.close()


app = FastAPI(title="Signalpipe", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------- auth ----------

def _sign(ts: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()


def _session_ok(cookie: str) -> bool:
    try:
        ts, sig = cookie.split(".", 1)
        return hmac.compare_digest(sig, _sign(ts)) and time.time() - float(ts) < 30 * 86400
    except Exception:
        return False


def auth(request: Request) -> None:
    if not config.APP_PASSWORD:
        return
    bearer = request.headers.get("authorization", "")
    if config.API_TOKEN and hmac.compare_digest(bearer, f"Bearer {config.API_TOKEN}"):
        return
    if _session_ok(request.cookies.get("sp_session", "")):
        return
    raise HTTPException(401, "Sign in required")


@app.post("/api/login")
async def login(response: Response, body: dict = Body(...)):
    await asyncio.sleep(0.4)  # blunt brute-force damping
    if not config.APP_PASSWORD or not hmac.compare_digest(str(body.get("password", "")), config.APP_PASSWORD):
        raise HTTPException(401, "Wrong password")
    ts = str(int(time.time()))
    response.set_cookie("sp_session", f"{ts}.{_sign(ts)}", max_age=30 * 86400, httponly=True, samesite="lax",
                        secure=config.env("COOKIE_SECURE", "0") == "1")
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("sp_session")
    return {"ok": True}


api = Depends(auth)


@app.get("/api/auth/check", dependencies=[api])
async def auth_check():
    """Used by the web server to guard the agent's screen: 200 for a signed-in session, 401 otherwise."""
    return {"ok": True}


# ---------- status ----------

@app.get("/api/status", dependencies=[api])
async def status():
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
    return {
        "config": config.status(), "linkedin": linkedin.state(), "llm_error": llm.last_error, "send_mode": db.get_setting("send_mode", "copilot"),
        "brain_ready": bool(brain_mod.get().get("one_liner")), "leads": counts,
        "plays": db.one("SELECT COUNT(*) n FROM plays")["n"],
        "signals_7d": db.one("SELECT COUNT(*) n FROM signals WHERE created_at>?", (db.now() - 7 * 86400,))["n"],
        "emails_today": outreach.sent_today("email"), "linkedin_today": outreach.sent_today("linkedin"),
        "calls_today": outreach.sent_today("call"),
        "unhandled_replies": db.one("SELECT COUNT(*) n FROM replies WHERE handled=0 AND kind LIKE 'reply%'")["n"],
        "sources": SOURCE_INFO,
    }


@app.put("/api/settings", dependencies=[api])
async def put_settings(body: dict = Body(...)):
    if body.get("send_mode") in ("copilot", "auto"):
        db.set_setting("send_mode", body["send_mode"])
    return {"send_mode": db.get_setting("send_mode", "copilot")}


# ---------- brain ----------

@app.get("/api/brain", dependencies=[api])
async def get_brain():
    return brain_mod.get()


@app.post("/api/brain/build", dependencies=[api])
async def build_brain(body: dict = Body(...)):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        b = await brain_mod.build(url)
    except Exception as e:
        raise HTTPException(400, f"Could not read that site: {e}")
    return {"brain": b, "suggested_play": brain_mod.suggested_play(b), "llm_error": llm.last_error}


@app.put("/api/brain", dependencies=[api])
async def put_brain(body: dict = Body(...)):
    return brain_mod.save(body)


# ---------- plays ----------

def _play(p: dict) -> dict:
    p["running"] = pipeline.is_running(p["id"])
    p["counts"] = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM leads WHERE play_id=? GROUP BY status", (p["id"],))}
    p["last_run"] = db.one("SELECT * FROM runs WHERE play_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
    return p


@app.get("/api/plays", dependencies=[api])
async def list_plays():
    return [_play(p) for p in db.q("SELECT * FROM plays ORDER BY id DESC")]


@app.post("/api/plays", dependencies=[api])
async def create_play(body: dict = Body(...)):
    cfg = {**brain_mod.suggested_play(brain_mod.get()), **(body.get("config") or {})}
    pid = db.x("INSERT INTO plays(name,config,created_at) VALUES(?,?,?)",
               (body.get("name") or "New play", db.j(cfg), db.now()))
    return _play(db.one("SELECT * FROM plays WHERE id=?", (pid,)))


@app.put("/api/plays/{pid}", dependencies=[api])
async def update_play(pid: int, body: dict = Body(...)):
    p = db.one("SELECT * FROM plays WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    db.x("UPDATE plays SET name=?, status=?, config=? WHERE id=?",
         (body.get("name", p["name"]), body.get("status", p["status"]),
          db.j({**p["config"], **(body.get("config") or {})}), pid))
    return _play(db.one("SELECT * FROM plays WHERE id=?", (pid,)))


@app.delete("/api/plays/{pid}", dependencies=[api])
async def delete_play(pid: int):
    for t in ("messages", "contacts"):
        db.x(f"DELETE FROM {t} WHERE lead_id IN (SELECT id FROM leads WHERE play_id=?)", (pid,))
    for t in ("signals", "leads", "runs", "plays"):
        db.x(f"DELETE FROM {t} WHERE {'id' if t == 'plays' else 'play_id'}=?", (pid,))
    return {"ok": True}


@app.post("/api/plays/{pid}/run", dependencies=[api])
async def run_play(pid: int, body: dict = Body(default={})):
    if not db.one("SELECT 1 FROM plays WHERE id=?", (pid,)):
        raise HTTPException(404)
    if pipeline.is_running(pid):
        return {"started": False, "reason": "already running"}
    if body.get("wait"):
        return await pipeline.run_play(pid, body.get("sources"))
    spawn(pipeline.run_play(pid, body.get("sources")))
    return {"started": True}


@app.post("/api/plays/{pid}/import", dependencies=[api])
async def import_csv(pid: int, request: Request):
    if not db.one("SELECT 1 FROM plays WHERE id=?", (pid,)):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > 5_000_000:
        raise HTTPException(413, "CSV is larger than 5 MB")
    return await pipeline.import_csv(pid, raw.decode("utf-8", "replace"))


@app.get("/api/runs", dependencies=[api])
async def runs(limit: int = 20):
    return db.q("SELECT r.*, p.name play_name FROM runs r LEFT JOIN plays p ON p.id=r.play_id ORDER BY r.id DESC LIMIT ?", (min(limit, 100),))


# ---------- leads ----------

def _lead_full(lead: dict) -> dict:
    lead["signals"] = db.q("SELECT id,source,type,title,text,url,occurred_at,extra FROM signals WHERE lead_id=? ORDER BY occurred_at DESC", (lead["id"],))
    lead["contacts"] = db.q("SELECT * FROM contacts WHERE lead_id=? ORDER BY is_primary DESC, id", (lead["id"],))
    for c in lead["contacts"]:
        if c["phone"] and c["phone_detail"].get("timezones"):
            c["phone_detail"]["call_window"] = validate.call_window(c["phone_detail"]["timezones"])
    lead["messages"] = db.q("SELECT * FROM messages WHERE lead_id=? ORDER BY channel, step", (lead["id"],))
    lead["replies"] = db.q("SELECT * FROM replies WHERE lead_id=? ORDER BY received_at DESC", (lead["id"],))
    title = next((c["title"] for c in lead["contacts"] if c["title"]), "") or (brain_mod.get()["icp"]["titles"] or [""])[0]
    lead["linkedin_search_url"] = enrich.linkedin_search_url(lead["company_name"] or "", title)
    return lead


@app.get("/api/leads", dependencies=[api])
async def list_leads(status: str = "new", play_id: int = 0, min_score: float = 0, q: str = "", limit: int = 100, offset: int = 0):
    where, params = ["1=1"], []
    if status != "all":
        where.append("l.status=?")
        params.append(status)
    if play_id:
        where.append("l.play_id=?")
        params.append(play_id)
    if min_score:
        where.append("l.score>=?")
        params.append(min_score)
    if q:
        where.append("(l.company_name LIKE ? OR l.company_domain LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql = f"FROM leads l WHERE {' AND '.join(where)}"
    total = db.one(f"SELECT COUNT(*) n {sql}", params)["n"]
    rows = db.q(
        f"SELECT l.*, (SELECT COUNT(*) FROM signals s WHERE s.lead_id=l.id) signal_count, "
        f"(SELECT s.type FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at DESC LIMIT 1) top_signal, "
        f"(SELECT s.title FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at DESC LIMIT 1) top_title, "
        f"(SELECT MAX(s.occurred_at) FROM signals s WHERE s.lead_id=l.id) last_signal_at "
        f"{sql} ORDER BY l.score DESC, l.id DESC LIMIT ? OFFSET ?", params + [min(limit, 500), offset])
    for r in rows:
        c = db.one("SELECT name,title,email,email_status,phone,linkedin_url FROM contacts WHERE lead_id=? ORDER BY is_primary DESC, id LIMIT 1", (r["id"],))
        r["contact"] = c
        r["has"] = {
            "email": bool(db.one("SELECT 1 FROM contacts WHERE lead_id=? AND email IS NOT NULL AND email_status!='invalid'", (r["id"],))),
            "phone": bool(db.one("SELECT 1 FROM contacts WHERE lead_id=? AND phone IS NOT NULL", (r["id"],))),
            "linkedin": bool(r["linkedin_company_url"] or (c and c["linkedin_url"])),
        }
    return {"total": total, "leads": rows}


@app.get("/api/leads/{lid}", dependencies=[api])
async def get_lead(lid: int):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lid,))
    if not lead:
        raise HTTPException(404)
    return _lead_full(lead)


@app.post("/api/leads/{lid}/approve", dependencies=[api])
async def approve(lid: int, body: dict = Body(default={})):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lid,))
    if not lead:
        raise HTTPException(404)
    if not lead["enriched_at"]:
        await enrich.enrich_lead(lid)
    db.x("UPDATE leads SET status='approved', note=COALESCE(?,note), updated_at=? WHERE id=?", (body.get("note"), db.now(), lid))
    res = await outreach.draft_sequence(lid, body.get("channels"))
    return {"lead": _lead_full(db.one("SELECT * FROM leads WHERE id=?", (lid,))), "sequence": res}


@app.post("/api/leads/{lid}/reject", dependencies=[api])
async def reject(lid: int, body: dict = Body(default={})):
    outreach.stop_sequence(lid, "lead rejected")
    db.x("UPDATE leads SET status='rejected', note=COALESCE(?,note), updated_at=? WHERE id=?", (body.get("note"), db.now(), lid))
    return {"ok": True}


@app.post("/api/leads/{lid}/status", dependencies=[api])
async def set_status(lid: int, body: dict = Body(...)):
    st = body.get("status")
    if st not in ("new", "approved", "rejected", "contacted", "replied", "won", "lost"):
        raise HTTPException(400, "bad status")
    if st in ("won", "lost", "rejected"):
        outreach.stop_sequence(lid, f"lead marked {st}")
    db.x("UPDATE leads SET status=?, note=COALESCE(?,note), updated_at=? WHERE id=?", (st, body.get("note"), db.now(), lid))
    return {"ok": True}


@app.post("/api/leads/{lid}/enrich", dependencies=[api])
async def re_enrich(lid: int):
    if not db.one("SELECT 1 FROM leads WHERE id=?", (lid,)):
        raise HTTPException(404)
    res = await enrich.enrich_lead(lid)
    await scoring.score_leads([lid])
    return {"result": res, "lead": _lead_full(db.one("SELECT * FROM leads WHERE id=?", (lid,)))}


@app.post("/api/leads/{lid}/redraft", dependencies=[api])
async def redraft(lid: int):
    return await outreach.draft_sequence(lid, replace=True)


@app.post("/api/leads/{lid}/contacts", dependencies=[api])
async def add_contact(lid: int, body: dict = Body(...)):
    lead = db.one("SELECT * FROM leads WHERE id=?", (lid,))
    if not lead:
        raise HTTPException(404)
    em = validate.normalize_email(body.get("email") or "")
    ed = await validate.validate_email(em) if em else {}
    ph = validate.validate_phone(body["phone"], validate.region_for_domain(lead["company_domain"] or "")) if body.get("phone") else {}
    cid = db.x("INSERT INTO contacts(lead_id,name,title,email,email_status,email_detail,phone,phone_detail,linkedin_url,source,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (lid, body.get("name", ""), body.get("title", ""), em or None, ed.get("status"), db.j(ed),
                ph.get("e164") or body.get("phone") or None, db.j(ph), body.get("linkedin_url", ""), "manual", db.now()))
    if body.get("primary", True):
        db.x("UPDATE contacts SET is_primary = CASE WHEN id=? THEN 1 ELSE 0 END WHERE lead_id=?", (cid, lid))
    return db.one("SELECT * FROM contacts WHERE id=?", (cid,))


@app.put("/api/contacts/{cid}", dependencies=[api])
async def update_contact(cid: int, body: dict = Body(...)):
    c = db.one("SELECT c.*, l.company_domain FROM contacts c JOIN leads l ON l.id=c.lead_id WHERE c.id=?", (cid,))
    if not c:
        raise HTTPException(404)
    if "email" in body:
        em = validate.normalize_email(body["email"] or "")
        ed = await validate.validate_email(em) if em else {}
        db.x("UPDATE contacts SET email=?, email_status=?, email_detail=? WHERE id=?", (em or None, ed.get("status"), db.j(ed), cid))
    if "phone" in body:
        ph = validate.validate_phone(body["phone"], validate.region_for_domain(c["company_domain"] or "")) if body["phone"] else {}
        db.x("UPDATE contacts SET phone=?, phone_detail=? WHERE id=?", (ph.get("e164") or body["phone"] or None, db.j(ph), cid))
    for f in ("name", "title", "linkedin_url"):
        if f in body:
            db.x(f"UPDATE contacts SET {f}=? WHERE id=?", (body[f], cid))
    if "do_not_contact" in body:
        db.x("UPDATE contacts SET do_not_contact=? WHERE id=?", (1 if body["do_not_contact"] else 0, cid))
    if body.get("primary"):
        db.x("UPDATE contacts SET is_primary = CASE WHEN id=? THEN 1 ELSE 0 END WHERE lead_id=?", (cid, c["lead_id"]))
    return db.one("SELECT * FROM contacts WHERE id=?", (cid,))


@app.get("/api/export.csv", dependencies=[api])
async def export_csv(status: str = "approved"):
    rows = db.q("SELECT l.company_name, l.company_domain, l.score, l.status, l.linkedin_company_url, c.name, c.title, c.email, "
                "c.email_status, c.phone, c.phone_detail, c.linkedin_url, "
                "(SELECT title FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at DESC LIMIT 1) signal "
                "FROM leads l LEFT JOIN contacts c ON c.lead_id=l.id AND c.is_primary=1 "
                + ("" if status == "all" else "WHERE l.status=? ") + "ORDER BY l.score DESC", () if status == "all" else (status,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "domain", "score", "status", "contact", "title", "email", "email_status", "phone", "phone_type",
                "linkedin", "company_linkedin", "signal"])
    for r in rows:
        w.writerow([r["company_name"], r["company_domain"], r["score"], r["status"], r["name"], r["title"], r["email"],
                    r["email_status"], r["phone"], (r["phone_detail"] or {}).get("type") if r["phone_detail"] else "",
                    r["linkedin_url"], r["linkedin_company_url"], r["signal"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="leads-{status}.csv"'})


# ---------- validation tools ----------

@app.post("/api/validate/email", dependencies=[api])
async def validate_email(body: dict = Body(...)):
    emails = body.get("emails") or [body.get("email", "")]
    return [await validate.validate_email(e) for e in emails[:200]]


@app.post("/api/validate/phone", dependencies=[api])
async def validate_phone(body: dict = Body(...)):
    phones = body.get("phones") or [body.get("phone", "")]
    return [validate.validate_phone(p, body.get("region", "")) for p in phones[:200]]


# ---------- outreach ----------

@app.get("/api/outreach/{channel}", dependencies=[api])
async def outreach_queue(channel: str, status: str = "open"):
    if channel not in ("email", "linkedin", "call"):
        raise HTTPException(404)
    cond = "m.status IN ('draft','queued','sending','failed')" if status == "open" else "m.status IN ('sent','done','skipped')"
    rows = db.q(
        f"SELECT m.*, l.company_name, l.company_domain, l.score, l.status lead_status, l.linkedin_company_url, "
        f"c.name contact_name, c.title contact_title, c.email, c.email_status, c.phone, c.phone_detail, c.linkedin_url "
        f"FROM messages m JOIN leads l ON l.id=m.lead_id LEFT JOIN contacts c ON c.id=m.contact_id "
        f"WHERE m.channel=? AND {cond} ORDER BY m.scheduled_at LIMIT 300", (channel,))
    for r in rows:
        if channel == "call" and r.get("phone_detail") and r["phone_detail"].get("timezones"):
            r["phone_detail"]["call_window"] = validate.call_window(r["phone_detail"]["timezones"])
        if channel == "linkedin":
            r["open_url"] = r["linkedin_url"] or r["linkedin_company_url"] or enrich.linkedin_search_url(r["company_name"] or "", r["contact_title"] or "", r["contact_name"] or "")
        r["due"] = (r["scheduled_at"] or 0) <= db.now()
    if channel == "call":
        rows.sort(key=lambda r: (not ((r.get("phone_detail") or {}).get("call_window") or {}).get("open_now"), -r["score"]))
    cap = {"email": config.EMAIL_DAILY_CAP, "linkedin": config.LINKEDIN_DAILY_CAP, "call": 0}[channel]
    return {"items": rows, "done_today": outreach.sent_today(channel), "daily_cap": cap}


@app.put("/api/messages/{mid}", dependencies=[api])
async def update_message(mid: int, body: dict = Body(...)):
    m = db.one("SELECT * FROM messages WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404)
    if m["status"] in ("sent", "done") and ("body" in body or "subject" in body):
        raise HTTPException(400, "already sent")
    for f in ("subject", "body"):
        if f in body:
            db.x(f"UPDATE messages SET {f}=? WHERE id=?", (body[f], mid))
    action = body.get("action")
    if action == "queue" and m["channel"] == "linkedin" and not linkedin.enabled():
        raise HTTPException(400, "No LinkedIn provider is connected; send this one by hand and mark it done")
    if action == "queue" and m["channel"] in ("email", "linkedin"):  # approve this step for automatic sending
        db.x("UPDATE messages SET status='queued', error=NULL WHERE id=?", (mid,))
    elif action == "queue_all" and m["channel"] == "email":  # approve the whole email sequence
        db.x("UPDATE messages SET status='queued', error=NULL WHERE lead_id=? AND channel='email' AND status IN ('draft','failed')", (m["lead_id"],))
    elif action == "unqueue":
        db.x("UPDATE messages SET status='draft' WHERE id=? AND status='queued'", (mid,))
    elif action == "skip":
        db.x("UPDATE messages SET status='skipped', error='skipped by you' WHERE id=?", (mid,))
    elif action == "done":  # linkedin and call steps are done by you, then logged here
        if m["channel"] == "linkedin" and outreach.sent_today("linkedin") >= config.LINKEDIN_DAILY_CAP:
            raise HTTPException(400, f"Daily LinkedIn cap of {config.LINKEDIN_DAILY_CAP} reached; this protects your account")
        db.x("UPDATE messages SET status='done', sent_at=?, outcome=? WHERE id=?", (db.now(), body.get("outcome", ""), mid))
        db.x("UPDATE leads SET status='contacted', updated_at=? WHERE id=? AND status='approved'", (db.now(), m["lead_id"]))
        oc = body.get("outcome", "")
        if oc == "meeting_booked":
            db.x("UPDATE leads SET status='won', updated_at=? WHERE id=?", (db.now(), m["lead_id"]))
            outreach.stop_sequence(m["lead_id"], "meeting booked")
        elif oc in ("not_interested", "do_not_call"):
            db.x("UPDATE leads SET status='lost', updated_at=? WHERE id=?", (db.now(), m["lead_id"]))
            outreach.stop_sequence(m["lead_id"], oc)
            if oc == "do_not_call" and m["contact_id"]:
                db.x("UPDATE contacts SET do_not_contact=1 WHERE id=?", (m["contact_id"],))
        elif oc == "bad_number" and m["contact_id"]:
            db.x("UPDATE contacts SET phone_detail=json_set(phone_detail,'$.callable',json('false'),'$.reported_bad',json('true')) WHERE id=?", (m["contact_id"],))
        elif oc in ("replied", "connected_interested"):
            db.x("UPDATE leads SET status='replied', updated_at=? WHERE id=?", (db.now(), m["lead_id"]))
    return db.one("SELECT * FROM messages WHERE id=?", (mid,))


@app.post("/api/outreach/send-now", dependencies=[api])
async def send_now():
    return await outreach.send_due()


@app.post("/api/outreach/test-email", dependencies=[api])
async def test_email(body: dict = Body(...)):
    """Send one plain message to yourself to confirm the SMTP settings work."""
    if not config.status()["smtp"]:
        raise HTTPException(400, "SMTP is not configured")
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, body.get("to") or config.SMTP_FROM, "Signalpipe test"
    msg.set_content("SMTP is working.")
    try:
        await asyncio.to_thread(outreach._smtp_send, msg)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"ok": True}


# ---------- linkedin connection ----------

@app.post("/api/linkedin/connect", dependencies=[api])
async def linkedin_connect(request: Request):
    try:
        return {"url": await linkedin.connect_url(str(request.base_url))}
    except linkedin.LinkedInError as e:
        raise HTTPException(400, str(e))


@app.post("/api/linkedin/refresh", dependencies=[api])
async def linkedin_refresh():
    try:
        return {**(await linkedin.refresh_account()), "state": linkedin.state()}
    except linkedin.LinkedInError as e:
        raise HTTPException(400, str(e))


@app.post("/api/linkedin/disconnect", dependencies=[api])
async def linkedin_disconnect():
    linkedin.disconnect()
    return linkedin.state()


@app.post("/api/linkedin/sync", dependencies=[api])
async def linkedin_sync():
    return await linkedin.sync()


# ---------- insights ----------

@app.get("/api/insights", dependencies=[api])
async def get_insights(play_id: int = 0):
    return insights.report(play_id or None)


# ---------- local linkedin agent ----------

@app.get("/api/agent/linkedin/work", dependencies=[api])
async def agent_work():
    return li_agent.next_work()


@app.post("/api/agent/linkedin/result", dependencies=[api])
async def agent_result(body: dict = Body(...)):
    return await li_agent.apply_result(body)


# ---------- inbox ----------

@app.get("/api/replies", dependencies=[api])
async def replies(handled: int = 0):
    return db.q("SELECT r.*, l.company_name, c.name contact_name, c.email contact_email FROM replies r "
                "LEFT JOIN leads l ON l.id=r.lead_id LEFT JOIN contacts c ON c.id=r.contact_id "
                "WHERE r.handled=? ORDER BY r.received_at DESC LIMIT 200", (handled,))


@app.put("/api/replies/{rid}", dependencies=[api])
async def update_reply(rid: int, body: dict = Body(...)):
    if "draft_reply" in body:
        db.x("UPDATE replies SET draft_reply=? WHERE id=?", (body["draft_reply"], rid))
    if "handled" in body:
        db.x("UPDATE replies SET handled=? WHERE id=?", (1 if body["handled"] else 0, rid))
    return db.one("SELECT * FROM replies WHERE id=?", (rid,))


@app.post("/api/replies/poll", dependencies=[api])
async def poll():
    return await outreach.poll_replies()


@app.get("/api/suppression", dependencies=[api])
async def suppression():
    return db.q("SELECT * FROM suppression ORDER BY created_at DESC")


@app.post("/api/suppression", dependencies=[api])
async def add_suppression(body: dict = Body(...)):
    v = (body.get("value") or "").strip().lower()
    if not v:
        raise HTTPException(400, "value is required")
    db.x("INSERT OR IGNORE INTO suppression(value,reason,created_at) VALUES(?,?,?)", (v, body.get("reason", "added by you"), db.now()))
    return {"ok": True}


@app.delete("/api/suppression/{value:path}", dependencies=[api])
async def del_suppression(value: str):
    db.x("DELETE FROM suppression WHERE value=?", (value.lower(),))
    return {"ok": True}


# ---------- seo ----------

@app.post("/api/seo/audit", dependencies=[api])
async def seo_audit(body: dict = Body(default={})):
    url = (body.get("url") or brain_mod.get().get("website") or "").strip()
    if not url:
        raise HTTPException(400, "Give a URL, or build the Brain first")
    try:
        return await seo.audit(url, max_pages=max(5, min(int(body.get("max_pages") or 30), 80)))
    except Exception as e:
        raise HTTPException(400, f"Audit failed: {type(e).__name__}: {e}")


@app.get("/api/seo/latest", dependencies=[api])
async def seo_latest():
    row = db.one("SELECT * FROM seo_audits ORDER BY id DESC LIMIT 1")
    history = db.q("SELECT id, url, created_at, json_extract(report,'$.score') score FROM seo_audits ORDER BY id DESC LIMIT 12")
    return {"latest": ({**row["report"], "created_at": row["created_at"]} if row else None), "history": history}


# ---------- static ----------

@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    path = os.path.join(STATIC, "index.html")
    if not os.path.exists(path):
        return PlainTextResponse("Signalpipe API is running. UI not built yet.")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})
