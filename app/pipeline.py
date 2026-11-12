"""Run a play end to end: collect signals, group them into leads, enrich, score."""
import asyncio
import csv
import io
import logging
import time
import traceback

from . import config, db, enrich, scoring
from .collectors import INTENT_WEIGHT, Signal, registry

log = logging.getLogger("signalpipe.pipeline")
_running: set[int] = set()


def lead_key(s: Signal) -> str:
    if s.company_domain:
        return "d:" + s.company_domain.lower()
    if s.company_name:
        return "c:" + (enrich.norm_company(s.company_name) or s.company_name.lower())
    if s.person_handle:
        return "p:" + s.person_handle.lower()
    if s.email:
        return "p:" + s.email.lower()
    return ""


def _handle_label(handle: str) -> str:
    site, _, user = (handle or "").partition(":")
    names = {"hn": "Hacker News user", "gh": "GitHub user", "li": "LinkedIn member"}
    return f"{names[site]} {user}" if site in names and user else handle


def ingest(play_id: int, signals: list[Signal]) -> tuple[int, int, set[int]]:
    """Store new signals and attach them to leads. Returns (new signals, new leads, touched lead ids)."""
    new_sig = new_leads = 0
    touched: set[int] = set()
    for s in signals:
        key = lead_key(s)
        if not key or db.one("SELECT 1 FROM signals WHERE play_id=? AND hash=?", (play_id, s.hash)):
            continue
        lead = db.one("SELECT id FROM leads WHERE play_id=? AND lead_key=?", (play_id, key))
        if lead:
            lead_id = lead["id"]
        else:
            lead_id = db.x("INSERT INTO leads(play_id,lead_key,company_name,company_domain,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?)",
                           (play_id, key, s.company_name or s.person_name or _handle_label(s.person_handle), s.company_domain,
                            db.now(), db.now()))
            new_leads += 1
        db.x("INSERT INTO signals(play_id,lead_id,source,type,title,text,url,occurred_at,hash,extra,created_at) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (play_id, lead_id, s.source, s.type, s.title, s.text, s.url, s.occurred_at or db.now(), s.hash,
              db.j(s.extra), db.now()))
        new_sig += 1
        touched.add(lead_id)
        if s.person_name or s.email or s.linkedin_url or s.phone:
            _contact_from_signal(lead_id, s)
    return new_sig, new_leads, touched


def _contact_from_signal(lead_id: int, s: Signal) -> None:
    dup = db.one("SELECT id FROM contacts WHERE lead_id=? AND (lower(name)=lower(?) OR (email IS NOT NULL AND email=?))",
                 (lead_id, s.person_name or "\0", s.email or "\0"))
    if dup:
        return
    db.x("INSERT INTO contacts(lead_id,name,title,email,phone,linkedin_url,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
         (lead_id, s.person_name, s.person_title, s.email.lower() or None, s.phone or None,
          s.linkedin_url or (s.url if s.source == "github" else ""), s.source, db.now()))


async def _validate_pending_contacts(lead_ids: list[int]) -> None:
    """Contacts that arrived with an address or number but no validation yet (imports, GitHub)."""
    from . import validate
    if not lead_ids:
        return
    marks = ",".join("?" * len(lead_ids))
    rows = db.q(f"SELECT c.*, l.company_domain FROM contacts c JOIN leads l ON l.id=c.lead_id "
                f"WHERE c.lead_id IN ({marks}) AND ((c.email IS NOT NULL AND c.email_status IS NULL) "
                f"OR (c.phone IS NOT NULL AND c.phone_detail='{{}}'))", lead_ids)
    for c in rows:
        if c["email"] and not c["email_status"]:
            d = await validate.validate_email(c["email"])
            db.x("UPDATE contacts SET email_status=?, email_detail=? WHERE id=?", (d["status"], db.j(d), c["id"]))
        if c["phone"] and not c["phone_detail"]:
            p = validate.validate_phone(c["phone"], validate.region_for_domain(c["company_domain"] or ""))
            db.x("UPDATE contacts SET phone=?, phone_detail=? WHERE id=?",
                 (p.get("e164") or c["phone"], db.j(p), c["id"]))


def merge_duplicates(play_id: int) -> int:
    """After enrichment a name-keyed lead may resolve to a domain another lead already owns. Fold them together."""
    merged = 0
    rows = db.q("SELECT company_domain, COUNT(*) n FROM leads WHERE play_id=? AND company_domain!='' "
                "GROUP BY company_domain HAVING n>1", (play_id,))
    for r in rows:
        dups = db.q("SELECT id, status FROM leads WHERE play_id=? AND company_domain=? ORDER BY "
                    "CASE status WHEN 'new' THEN 1 ELSE 0 END, id", (play_id, r["company_domain"]))
        keep = dups[0]["id"]
        for d in dups[1:]:
            if d["status"] != "new":
                continue  # never fold away a lead you have already acted on
            db.x("UPDATE signals SET lead_id=? WHERE lead_id=?", (keep, d["id"]))
            db.x("UPDATE contacts SET lead_id=?, is_primary=0 WHERE lead_id=? AND lower(coalesce(name,'')||coalesce(email,'')) "
                 "NOT IN (SELECT lower(coalesce(name,'')||coalesce(email,'')) FROM contacts WHERE lead_id=?)",
                 (keep, d["id"], keep))
            db.x("DELETE FROM contacts WHERE lead_id=?", (d["id"],))
            db.x("DELETE FROM leads WHERE id=?", (d["id"],))
            merged += 1
    return merged


async def run_play(play_id: int, sources: list[str] | None = None) -> dict:
    if play_id in _running:
        return {"skipped": "already running"}
    play = db.one("SELECT * FROM plays WHERE id=?", (play_id,))
    if not play:
        return {"error": "no such play"}
    _running.add(play_id)
    run_id = db.x("INSERT INTO runs(play_id,started_at) VALUES(?,?)", (play_id, db.now()))
    stats: dict = {"sources": {}, "new_signals": 0, "new_leads": 0, "enriched": 0, "scored": 0, "merged": 0}
    error = None
    try:
        cfg = play["config"]
        reg = registry()
        chosen = [s for s in (sources or cfg.get("sources") or list(reg)) if s in reg]
        results = await asyncio.gather(*[reg[s](cfg) for s in chosen], return_exceptions=True)
        touched: set[int] = set()
        for name, res in zip(chosen, results):
            if isinstance(res, Exception):
                stats["sources"][name] = f"error: {type(res).__name__}: {str(res)[:120]}"
                continue
            ns, nl, t = ingest(play_id, res)
            stats["sources"][name] = {"found": len(res), "new": ns}
            stats["new_signals"] += ns
            stats["new_leads"] += nl
            touched |= t
        db.x("UPDATE runs SET stats=? WHERE id=?", (db.j({**stats, "stage": "enriching"}), run_id))

        # Enrich the most promising unenriched leads first, bounded per run to stay polite and cheap.
        pending = db.q("SELECT l.id, MAX(s.occurred_at) newest, GROUP_CONCAT(DISTINCT s.type) types FROM leads l "
                       "JOIN signals s ON s.lead_id=l.id WHERE l.play_id=? AND l.enriched_at IS NULL AND l.status='new' "
                       "GROUP BY l.id", (play_id,))
        pending.sort(key=lambda r: (-max(INTENT_WEIGHT.get(t, 40) for t in r["types"].split(",")), -r["newest"]))
        sem = asyncio.Semaphore(5)

        async def _one(lid: int):
            async with sem:
                try:
                    await enrich.enrich_lead(lid)
                    return True
                except Exception:
                    log.warning("enrich failed for lead %s: %s", lid, traceback.format_exc(limit=2))
                    db.x("UPDATE leads SET enriched_at=? WHERE id=?", (db.now(), lid))
                    return False
        batch = [r["id"] for r in pending[: config.ENRICH_PER_RUN]]
        done = await asyncio.gather(*[_one(i) for i in batch])
        stats["enriched"] = sum(1 for d in done if d)
        stats["enrich_backlog"] = max(0, len(pending) - len(batch))
        stats["merged"] = merge_duplicates(play_id)
        await _validate_pending_contacts(list(touched))
        for lid in touched:
            enrich.set_primary(lid)

        alive = [r["id"] for r in db.q(
            "SELECT id FROM leads WHERE play_id=? AND status='new' AND (id IN (%s) OR score=0)" %
            (",".join(str(i) for i in touched | set(batch)) or "0"), (play_id,))]
        db.x("UPDATE runs SET stats=? WHERE id=?", (db.j({**stats, "stage": "scoring"}), run_id))
        stats["scored"] = await scoring.score_leads(alive)
        db.x("UPDATE plays SET last_run_at=? WHERE id=?", (db.now(), play_id))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.error("play %s failed: %s", play_id, traceback.format_exc())
    finally:
        _running.discard(play_id)
        stats["seconds"] = round(time.time() - (db.one("SELECT started_at FROM runs WHERE id=?", (run_id,)) or {}).get("started_at", time.time()), 1)
        db.x("UPDATE runs SET finished_at=?, stats=?, error=? WHERE id=?", (db.now(), db.j(stats), error, run_id))
    return {"run_id": run_id, "error": error, **stats}


def is_running(play_id: int) -> bool:
    return play_id in _running


# Column names seen in LinkedIn's own "Connections" export, Sales Navigator exports, and common CRM CSVs.
CSV_MAP = {
    "first": ("first name", "firstname", "first"),
    "last": ("last name", "lastname", "last"),
    "name": ("name", "full name", "contact", "contact name"),
    "company": ("company", "company name", "organization", "account", "account name"),
    "title": ("position", "title", "job title", "role"),
    "email": ("email address", "email", "e-mail", "work email"),
    "phone": ("phone", "phone number", "mobile", "direct phone", "work phone"),
    "linkedin": ("url", "profile url", "linkedin", "linkedin url", "person linkedin url", "linkedin profile"),
    "domain": ("website", "domain", "company website", "company domain"),
}


def parse_csv(text: str) -> list[Signal]:
    # LinkedIn's export starts with a few lines of notes before the header row.
    lines = text.lstrip("﻿").splitlines()
    start = next((i for i, l in enumerate(lines[:10]) if "," in l and
                  any(k in l.lower() for k in ("first name", "email", "company", "name"))), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for row in reader:
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

        def pick(field):
            return next((low[c] for c in CSV_MAP[field] if low.get(c)), "")
        name = pick("name") or " ".join(x for x in (pick("first"), pick("last")) if x)
        company, email = pick("company"), pick("email")
        if not (name or company or email):
            continue
        dom = pick("domain")
        out.append(Signal(
            source="csv", type="import", title=f"Imported: {name or email} at {company or '?'}",
            url=pick("linkedin") or f"csv:{name}:{company}:{email}", occurred_at=time.time(),
            company_name=company, company_domain=enrich.web.host_of(dom) if dom else "",
            person_name=name, person_title=pick("title"), email=email, phone=pick("phone"),
            linkedin_url=pick("linkedin"), person_handle=(email or name).lower(),
        ))
    return out


async def import_csv(play_id: int, text: str) -> dict:
    sigs = parse_csv(text)
    ns, nl, touched = ingest(play_id, sigs)
    await _validate_pending_contacts(list(touched))
    for lid in touched:
        enrich.set_primary(lid)
    scored = await scoring.score_leads(list(touched))
    return {"rows": len(sigs), "new_leads": nl, "new_signals": ns, "scored": scored}
