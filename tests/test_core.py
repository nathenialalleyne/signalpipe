"""Offline tests: no network, no API keys. External services are faked at their boundaries."""
import asyncio
import os
import time

import pytest

os.environ["SIGNALPIPE_SCHEDULER"] = "0"
os.environ["SIGNALPIPE_LLM"] = "0"

from app import brain, config, db, linkedin, outreach, pipeline, scoring, seo, validate  # noqa: E402
from app.collectors import Signal  # noqa: E402
from app.collectors.funding import parse_headline  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.reset_for_tests(str(tmp_path / "t.db"))
    db.conn()
    db.cache_set("mx:acme.io", ["mx.acme.io"])
    db.cache_set("mx:nomail.io", [])
    brain.save({"company": "Sender Co", "one_liner": "We fix outbound.", "keywords": ["cold email"],
                "competitors": ["HubSpot"], "pains": ["Reps waste time prospecting"], "sender_name": "Sam",
                "icp": {"titles": ["Head of Sales"], "industries": ["B2B SaaS"]}})
    yield
    db.reset_for_tests(str(tmp_path / "t.db"))


def run(coro):
    return asyncio.run(coro)


def make_play(**cfg):
    return db.x("INSERT INTO plays(name,config,created_at) VALUES(?,?,?)",
                ("p", db.j({"channels": ["email", "linkedin", "phone"], **cfg}), db.now()))


def seed_lead(play_id, email="dana@acme.io", phone="+14155550132", linkedin_url="https://www.linkedin.com/in/dana-x"):
    sig = Signal(source="jobs", type="hiring", title="Acme is hiring: SDR", url="https://x/1", occurred_at=time.time(),
                 company_name="Acme", company_domain="acme.io", person_name="Dana Reyes", person_title="Head of Sales",
                 email=email, phone=phone, linkedin_url=linkedin_url, extra={"role": "SDR"})
    pipeline.ingest(play_id, [sig])
    lead = db.one("SELECT * FROM leads WHERE play_id=?", (play_id,))
    run(pipeline._validate_pending_contacts([lead["id"]]))
    db.x("UPDATE leads SET enriched_at=?, status='approved' WHERE id=?", (db.now(), lead["id"]))
    return lead["id"]


# ---------- validation ----------

def test_email_validation_statuses():
    assert run(validate.validate_email("not-an-email"))["status"] == "invalid"
    assert run(validate.validate_email("x@mailinator.com"))["flags"] == ["disposable"]
    assert run(validate.validate_email("dana@nomail.io"))["reason"] == "domain has no mail servers"
    ok = run(validate.validate_email("Dana@Acme.io "))
    assert ok["email"] == "dana@acme.io" and ok["status"] == "risky"  # MX only: never claimed as verified
    role = run(validate.validate_email("info@acme.io"))
    assert "role" in role["flags"] and role["score"] < ok["score"]


def test_suppressed_address_is_invalid():
    db.x("INSERT INTO suppression(value,reason,created_at) VALUES('acme.io','x',?)", (db.now(),))
    assert run(validate.validate_email("dana@acme.io"))["flags"] == ["suppressed"]


def test_phone_validation():
    us = validate.validate_phone("(415) 555-0132", "US")
    assert us["valid"] and us["e164"] == "+14155550132" and "America/Los_Angeles" in us["timezones"]
    tf = validate.validate_phone("+1 800 203 6755")
    assert tf["type"] == "toll_free" and "main_line" in tf["flags"]
    uk = validate.validate_phone("07400 123456", "GB")
    assert uk["valid"] and uk["type"] == "mobile" and uk["region"] == "GB"
    assert not validate.validate_phone("12345", "US")["valid"]


def test_call_window_respects_local_business_hours():
    import datetime as dt
    noon_utc = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)  # a Wednesday
    assert validate.call_window(["Europe/London"], noon_utc)["open_now"] is True
    assert validate.call_window(["America/Los_Angeles"], noon_utc)["open_now"] is False  # 4am there
    sat = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)
    assert validate.call_window(["Europe/London"], sat)["open_now"] is False


def test_email_candidates():
    assert validate.email_candidates("Dana Reyes", "acme.io")[:2] == ["dana.reyes@acme.io", "dana@acme.io"]


# ---------- collectors and ingest ----------

@pytest.mark.parametrize("headline,company", [
    ("Alta raises $48 million in Series A round led by Index - Fortune", "Alta"),
    ("Fintech startup Coast raises $35 million to track spending", "Coast"),
    ("CopperPilot, an AI design agent startup, raises $390,000 in funding", "CopperPilot"),
    ("Proximal Ventures raises debut fund to back startups", None),
    ("Entourage closes €100 million second fund", None),
])
def test_funding_headlines(headline, company):
    got = parse_headline(headline)
    assert (got or {}).get("company") == company


def test_ingest_dedupes_and_groups_by_company():
    pid = make_play()
    a = Signal(source="jobs", type="hiring", title="Acme is hiring: SDR", url="u1", occurred_at=time.time(), company_name="Acme Inc.")
    b = Signal(source="funding", type="funding", title="Acme raises $5M", url="u2", occurred_at=time.time(), company_name="Acme")
    assert pipeline.ingest(pid, [a, b, a])[:2] == (2, 1)
    assert pipeline.ingest(pid, [a])[:2] == (0, 0)
    run(scoring.score_leads([db.one("SELECT id FROM leads")["id"]]))
    lead = db.one("SELECT * FROM leads")
    assert lead["intent"] > 75 and lead["score"] > 0  # two signal kinds stack


def test_csv_import_reads_linkedin_export():
    pid = make_play()
    text = ("Notes:\n\"When exporting your connection data...\"\n\n"
            "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            "Dana,Reyes,https://www.linkedin.com/in/dana-x,dana@acme.io,Acme,Head of Sales,01 Jan 2026\n"
            "Lee,Park,https://www.linkedin.com/in/lee-p,,Beta Corp,CEO,02 Jan 2026\n")
    res = run(pipeline.import_csv(pid, text))
    assert res["rows"] == 2 and res["new_leads"] == 2
    c = db.one("SELECT * FROM contacts WHERE name='Dana Reyes'")
    assert c["email_status"] == "risky" and c["linkedin_url"].endswith("dana-x") and c["is_primary"] == 1


# ---------- outreach ----------

def test_sequence_and_reply_stops_it():
    lid = seed_lead(make_play())
    res = run(outreach.draft_sequence(lid))
    assert res["created"] == ["email1", "email2", "email3", "linkedin1", "linkedin2", "call"]
    assert db.one("SELECT COUNT(*) n FROM messages WHERE status='draft'")["n"] == 6  # copilot: nothing sends unreviewed
    note = db.one("SELECT body FROM messages WHERE channel='linkedin' AND step=1")["body"]
    assert len(note) <= outreach.LINKEDIN_NOTE_MAX
    db.x("UPDATE messages SET status='sent', sent_at=? WHERE channel='email' AND step=1", (db.now(),))
    db.x("UPDATE leads SET status='contacted'")
    got = run(outreach.handle_inbound("Dana <dana@acme.io>", "Re: hi", "Sure, send details.", "<m1@acme.io>"))
    assert got["kind"] == "reply"
    assert db.one("SELECT status FROM leads")["status"] == "replied"
    assert db.one("SELECT COUNT(*) n FROM messages WHERE status IN ('draft','queued')")["n"] == 0
    assert run(outreach.handle_inbound("Dana <dana@acme.io>", "Re: hi", "again", "<m1@acme.io>")) == {"duplicate": True}


def test_unsubscribe_and_bounce():
    lid = seed_lead(make_play())
    run(outreach.draft_sequence(lid))
    run(outreach.handle_inbound("dana@acme.io", "Re: hi", "Please remove me from your list", "<u1>"))
    assert db.one("SELECT 1 FROM suppression WHERE value='dana@acme.io'")
    assert db.one("SELECT do_not_contact d FROM contacts")["d"] == 1
    assert db.one("SELECT status FROM leads")["status"] == "lost"

    lid2 = seed_lead(make_play(), email="bo@acme.io")
    run(outreach.handle_inbound("MAILER-DAEMON@mx.acme.io", "Undelivered Mail Returned to Sender",
                                "<bo@acme.io>: user unknown", "<b1>"))
    assert db.one("SELECT email_status s FROM contacts WHERE email='bo@acme.io'")["s"] == "invalid"
    assert lid2


def test_send_due_over_real_smtp(monkeypatch):
    from aiosmtpd.controller import Controller

    class Sink:
        got = []

        async def handle_DATA(self, server, session, envelope):
            Sink.got.append((envelope.rcpt_tos, envelope.content.decode()))
            return "250 OK"

    ctl = Controller(Sink(), hostname="127.0.0.1", port=8925)
    ctl.start()
    try:
        for k, v in dict(SMTP_HOST="127.0.0.1", SMTP_PORT=8925, SMTP_FROM="sam@sender.co", SMTP_SECURITY="none",
                         SMTP_USER="", POSTAL_ADDRESS="1 Main St, Springfield").items():
            monkeypatch.setattr(config, k, v)

        async def no_sleep(_):
            return None
        monkeypatch.setattr(outreach.asyncio, "sleep", no_sleep)
        lid = seed_lead(make_play())
        run(outreach.draft_sequence(lid))
        assert run(outreach.send_due())["sent"] == 0          # drafts never send
        db.x("UPDATE messages SET status='queued' WHERE channel='email'")
        assert run(outreach.send_due())["sent"] == 1          # only step 1 is due today
        rcpt, raw = Sink.got[0]
        assert rcpt == ["dana@acme.io"] and "unsubscribe" in raw and "1 Main St" in raw and "List-Unsubscribe" in raw
        assert db.one("SELECT status FROM leads")["status"] == "contacted"
        db.x("INSERT INTO suppression(value,reason,created_at) VALUES('dana@acme.io','x',?)", (db.now(),))
        db.x("UPDATE messages SET scheduled_at=0 WHERE status='queued'")
        assert run(outreach.send_due())["sent"] == 0          # suppression wins over the queue
        assert db.one("SELECT COUNT(*) n FROM messages WHERE status='skipped'")["n"] == 2
    finally:
        ctl.stop()


def test_invalid_email_gets_no_email_steps():
    lid = seed_lead(make_play(), email="dana@nomail.io")
    assert "email1" not in run(outreach.draft_sequence(lid))["created"]


# ---------- linkedin provider (faked at the HTTP boundary) ----------

class FakeUnipile:
    def __init__(self):
        self.calls, self.relations, self.messages, self.fail = [], [], [], None

    async def __call__(self, method, path, params=None, json=None, files=None):
        self.calls.append((method, path, json or files))
        if self.fail and path == "/users/invite":
            raise linkedin.LinkedInError(422, self.fail)
        if path == "/linkedin/company/hubspot":
            return {"id": "1234"}
        if path == "/users/1234/posts":
            return {"items": [{"social_id": "urn:li:activity:9", "share_url": "https://li/p/9", "text": "Big launch",
                               "parsed_datetime": "2099-01-01T00:00:00.000Z", "comment_counter": 1, "reaction_counter": 2}]}
        if path.endswith("/comments"):
            return {"items": [{"author": "Ana Ruiz", "text": "How does pricing compare?",
                               "author_details": {"id": "ACoANA", "headline": "VP Sales at Globex | SaaS",
                                                  "profile_url": "https://www.linkedin.com/in/ana-ruiz"}}]}
        if path.endswith("/reactions"):
            return {"items": [{"value": "LIKE", "author": {"id": "ACoBEN", "type": "INDIVIDUAL", "name": "Ben Ode",
                                                           "headline": "Founder @ Initech", "profile_url": "https://www.linkedin.com/in/ben-ode"}},
                              {"value": "LIKE", "author": {"id": "c1", "type": "COMPANY", "name": "Some Page", "headline": None, "profile_url": None}}]}
        if path == "/linkedin/search":
            return {"items": []}
        if path == "/users/dana-x":
            return {"provider_id": "ACoDANA"}
        if path == "/users/invite":
            return {"object": "UserInvitationSent", "invitation_id": "inv1"}
        if path == "/chats":
            return {"object": "ChatStarted", "chat_id": "chat1", "message_id": "m1"}
        if path == "/users/relations":
            return {"items": self.relations}
        if path == "/messages":
            return {"items": self.messages}
        if path == "/accounts":
            return {"items": [{"id": "acc9", "type": "LINKEDIN", "name": "Sam"}]}
        if path == "/hosted/accounts/link":
            return {"object": "HostedAuthURL", "url": "https://account.unipile.com/abc"}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def li(monkeypatch):
    fake = FakeUnipile()
    monkeypatch.setattr(linkedin, "DSN", "api1.unipile.com:13111")
    monkeypatch.setattr(linkedin, "API_KEY", "k")
    monkeypatch.setattr(linkedin, "_req", fake)
    monkeypatch.setattr(linkedin, "_working_hours", lambda now=None: True)
    monkeypatch.setattr(linkedin.random, "random", lambda: 0.99)

    async def no_pause():
        return None
    monkeypatch.setattr(linkedin, "_pause", no_pause)
    return fake


def test_linkedin_hosted_login(li):
    assert not linkedin.enabled()
    assert run(linkedin.connect_url("https://app.example/")) == "https://account.unipile.com/abc"
    body = li.calls[-1][2]
    assert body["providers"] == ["LINKEDIN"] and body["api_url"] == "https://api1.unipile.com:13111"
    assert run(linkedin.refresh_account())["connected"] and linkedin.enabled()
    linkedin.disconnect()
    assert not linkedin.enabled()


def test_linkedin_engagement_signals(li):
    run(linkedin.refresh_account())
    sigs = run(linkedin.collect({"linkedin_watch": ["https://www.linkedin.com/company/hubspot/"], "lookback_days": 14}))
    assert [(s.person_name, s.company_name, s.extra["action"]) for s in sigs] == [
        ("Ana Ruiz", "Globex", "comment"), ("Ben Ode", "Initech", "reaction")]  # company pages are dropped
    pid = make_play()
    assert pipeline.ingest(pid, sigs)[:2] == (2, 2)
    c = db.one("SELECT * FROM contacts WHERE name='Ana Ruiz'")
    assert c["linkedin_url"] == "https://www.linkedin.com/in/ana-ruiz" and c["title"].startswith("VP Sales")


def test_linkedin_invite_accept_followup_reply(li):
    run(linkedin.refresh_account())
    lid = seed_lead(make_play())
    run(outreach.draft_sequence(lid))
    assert run(linkedin.send_next())["sent"] == 0             # nothing approved yet
    db.x("UPDATE messages SET status='queued' WHERE channel='linkedin' AND step=1")
    assert run(linkedin.send_next()) == {"sent": 1, "step": 1}
    invite = [c for c in li.calls if c[1] == "/users/invite"][0][2]
    assert invite["provider_id"] == "ACoDANA" and len(invite["message"]) <= linkedin.NOTE_MAX
    assert run(linkedin.sync())["accepted"] == 0              # not connected yet
    li.relations = [{"member_id": "ACoDANA", "public_identifier": "dana-x"}]
    assert run(linkedin.sync())["accepted"] == 1
    step2 = db.one("SELECT * FROM messages WHERE channel='linkedin' AND step=2")
    assert step2["status"] == "draft"                         # copilot mode: follow-up waits for review
    db.x("UPDATE messages SET status='queued', scheduled_at=0 WHERE id=?", (step2["id"],))
    assert run(linkedin.send_next()) == {"sent": 1, "step": 2}
    li.messages = [{"is_sender": 0, "text": "Happy to chat next week", "chat_id": "chat1", "id": "x1", "sender_id": "ACoDANA"}]
    assert run(linkedin.sync())["replies"] == 1
    assert db.one("SELECT status FROM leads")["status"] == "replied"
    assert db.one("SELECT COUNT(*) n FROM messages WHERE status IN ('draft','queued')")["n"] == 0


def test_linkedin_limit_pauses_and_caps_hold(li, monkeypatch):
    run(linkedin.refresh_account())
    lid = seed_lead(make_play())
    run(outreach.draft_sequence(lid))
    db.x("UPDATE messages SET status='queued' WHERE channel='linkedin' AND step=1")
    monkeypatch.setattr(config, "LINKEDIN_DAILY_CAP", 0)
    assert run(linkedin.send_next())["reason"] == "invite cap reached"
    monkeypatch.setattr(config, "LINKEDIN_DAILY_CAP", 20)
    li.fail = "cannot_resend_yet"
    assert "paused" in run(linkedin.send_next())["reason"]
    li.fail = None
    assert run(linkedin.send_next())["reason"].startswith("paused")   # stays paused; no retry storm
    assert not [c for c in li.calls if c[1] == "/chats"]


# ---------- seo ----------

def test_seo_page_checks():
    html = "<html><head><title>Hi</title></head><body><h2>x</h2><img src=a.png><p>short</p></body></html>"
    codes = {i["code"] for i in seo._page_report("https://s.io/", 200, html, 2000, "s.io")["issues"]}
    assert {"title_short", "description_missing", "h1_missing", "thin_content", "viewport_missing", "slow", "lang_missing"} <= codes
    good = ("<html lang=en><head><title>Cold email software for B2B sales teams</title>"
            "<meta name=description content='" + "x" * 130 + "'><meta name=viewport content=w>"
            "<link rel=canonical href='https://s.io/'><meta property='og:title' content=t><meta property='og:image' content=i>"
            "</head><body><h1>Cold email</h1><p>" + "word " * 300 + "</p></body></html>")
    rep = seo._page_report("https://s.io/", 200, good, 100, "s.io")
    assert rep["issues"] == []
    cov = seo.keyword_coverage({"pages": [rep]}, brain.get())
    assert {c["keyword"]: c["status"] for c in cov}["cold email"] == "covered"
    assert {c["keyword"]: c["status"] for c in cov}["HubSpot alternative"] == "missing"
