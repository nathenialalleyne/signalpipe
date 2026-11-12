"""Lead score = ICP fit (who they are) + intent (what they did) + recency (how fresh it is)."""
import re
import time

from . import brain as brain_mod
from . import db, llm
from .collectors import INTENT_WEIGHT

W_FIT, W_INTENT, W_RECENCY = 0.5, 0.3, 0.2
GENERIC_WORDS = {"business", "company", "customer", "customers", "platform", "software", "solution", "solutions",
                 "service", "services", "product", "products", "manual", "better", "faster", "growth", "people",
                 "process", "online", "digital", "management", "system", "systems", "teams", "tools", "working"}
HALF_LIFE_DAYS = 10.0

FIT_SCHEMA = llm.obj({"leads": llm.arr(llm.obj({
    "id": llm.INT, "fit": llm.INT, "reason": llm.STR, "angle": llm.STR,
}))})

FIT_SYSTEM = """You qualify sales leads for the company described below. For each lead, judge how well it matches
the ideal customer profile and whether the observed signal reflects a real need for what this company sells.

Score fit from 0 to 100:
- 80-100: clearly inside the ICP and the signal points at the exact problem this company solves.
- 50-79: plausible buyer, or right company with an indirect signal.
- 20-49: weak or unclear match.
- 0-19: wrong kind of company, a competitor, a job seeker, a vendor pitching, or an irrelevant mention.

Judge on evidence in the lead, not on optimism. A lead with almost no information scores in the 30s, not higher.
reason: one sentence a salesperson can read in the queue, naming the evidence.
angle: one sentence on how to open the conversation given the signal. Empty string when fit is under 40.
Return every lead id you were given."""


def intent_and_recency(signals: list[dict], lead: dict, comp_names: list[str]) -> tuple[float, float, list[str]]:
    if not signals:
        return 0.0, 0.0, []
    weights, reasons, kinds = [], [], set()
    for s in signals:
        w = INTENT_WEIGHT.get(s["type"], 40)
        ex = s.get("extra") or {}
        if ex.get("asks"):
            w += 5
        if ex.get("watchlist"):
            w += 8
        weights.append(min(100, w))
        kinds.add((s["source"], s["type"]))
    intent = max(weights) + 6 * (len(kinds) - 1) + min(8, 2 * (len(signals) - len(kinds)))
    comp_low = [c.lower() for c in comp_names]
    uses = [t for t in (lead.get("tech") or []) if t.lower() in comp_low]
    if uses:
        intent += 10
        reasons.append(f"Site runs {', '.join(uses)}, a tool you compete with")
    newest = max(s["occurred_at"] for s in signals)
    age_days = max(0.0, (time.time() - newest) / 86400)
    recency = 100 * 0.5 ** (age_days / HALF_LIFE_DAYS)
    top = sorted(signals, key=lambda s: -INTENT_WEIGHT.get(s["type"], 40))[0]
    age = "today" if age_days < 1 else f"{age_days:.0f}d ago"
    reasons.insert(0, f"{top['type'].replace('_', ' ').title()}: {top['title'][:110]} ({age})")
    if len(kinds) > 1:
        reasons.append(f"{len(signals)} signals of {len(kinds)} different kinds")
    elif len(signals) > 1:
        reasons.append(f"{len(signals)} signals of the same kind")
    return min(100.0, intent), recency, reasons


def heuristic_fit(lead: dict, signals: list[dict], brain: dict) -> tuple[float, str]:
    hay = " ".join([lead.get("description") or "", lead.get("company_name") or ""] +
                   [f"{s['title']} {s.get('text') or ''}" for s in signals]).lower()
    for bad in brain["icp"].get("disqualifiers", []):
        if bad and bad.lower() in hay:
            return 10.0, f"Matches disqualifier: {bad}"
    # Whole phrases count; single words only when they are distinctive. Deliberately conservative:
    # without Claude this is a keyword match, and it should not pretend to be a judgement.
    phrases = [p.lower() for g in (brain.get("keywords", []), brain["icp"].get("industries", [])) for p in g if p]
    hits = sorted({p for p in phrases if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", hay)})
    words = set()
    for g in (brain.get("keywords", []), brain.get("pains", []), brain["icp"].get("industries", [])):
        for p in g:
            words.update(w for w in re.findall(r"[a-z]{6,}", p.lower()) if w not in GENERIC_WORDS)
    word_hits = sorted(w for w in words if w in hay and not any(w in h for h in hits))
    if not lead.get("description") and not hits:
        return 35.0, "Little public information; keyword match only"
    fit = min(75.0, 35.0 + 12.0 * len(hits) + 3.0 * len(word_hits))
    found = hits + word_hits
    return fit, ("Mentions " + ", ".join(found[:5])) if found else "No ICP keywords found on their site"


def _lead_block(lead: dict, signals: list[dict], contact: dict | None) -> str:
    lines = [f"id: {lead['id']}", f"company: {lead.get('company_name') or '(unknown)'} {lead.get('company_domain') or ''}"]
    if lead.get("description"):
        lines.append(f"about: {lead['description'][:300]}")
    if lead.get("tech"):
        lines.append(f"tech on site: {', '.join(lead['tech'][:12])}")
    if contact and (contact.get("name") or contact.get("title")):
        lines.append(f"contact: {contact.get('name') or ''} {contact.get('title') or ''}".strip())
    for s in signals[:3]:
        lines.append(f"signal [{s['type']}] {s['title'][:160]} :: {(s.get('text') or '')[:350]}")
    return "\n".join(lines)


def _examples() -> str:
    """Your past approve/reject decisions, so scoring drifts toward what you actually accept."""
    out = []
    for status, label in (("approved", "APPROVED by the user"), ("rejected", "REJECTED by the user")):
        rows = db.q("SELECT l.company_name, l.description, l.note, "
                    "(SELECT title FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at DESC LIMIT 1) AS sig "
                    "FROM leads l WHERE l.status IN (?, ?) ORDER BY l.updated_at DESC LIMIT 6",
                    (status, "contacted" if status == "approved" else status))
        for r in rows:
            note = f" (note: {r['note']})" if r.get("note") else ""
            out.append(f"- {label}: {r['company_name'] or '?'}: {(r['description'] or '')[:120]} | {r['sig'] or ''}{note}")
    return "\n".join(out)


async def score_leads(lead_ids: list[int]) -> int:
    brain = brain_mod.get()
    leads = []
    for lid in lead_ids:
        lead = db.one("SELECT * FROM leads WHERE id=?", (lid,))
        if lead:
            sigs = db.q("SELECT * FROM signals WHERE lead_id=? ORDER BY occurred_at DESC", (lid,))
            contact = db.one("SELECT * FROM contacts WHERE lead_id=? ORDER BY is_primary DESC LIMIT 1", (lid,))
            leads.append((lead, sigs, contact))
    fits: dict[int, tuple[float, str, str]] = {}
    if llm.available() and leads:
        examples = _examples()
        system = FIT_SYSTEM + "\n\n# Company\n" + brain_mod.summary(brain)
        if examples:
            system += "\n\n# The user's recent decisions (calibrate to these)\n" + examples
        for i in range(0, len(leads), 12):
            batch = leads[i:i + 12]
            prompt = "\n\n---\n\n".join(_lead_block(l, s, c) for l, s, c in batch)
            data = await llm.ask_json(system, prompt, FIT_SCHEMA, effort="low", max_tokens=6000)
            for row in (data or {}).get("leads", []):
                fits[int(row["id"])] = (float(max(0, min(100, row["fit"]))), row["reason"], row.get("angle", ""))
    done = 0
    for lead, sigs, _ in leads:
        intent, recency, reasons = intent_and_recency(sigs, lead, brain.get("competitors", []))
        if lead["id"] in fits:
            fit, why, angle = fits[lead["id"]]
            method = "claude"
        else:
            fit, why = heuristic_fit(lead, sigs, brain)
            angle, method = "", "keywords"
        total = W_FIT * fit + W_INTENT * intent + W_RECENCY * recency
        if fit < 20:
            total = min(total, 25)  # a wrong-fit company never ranks on intent alone
        all_reasons = [f"Fit {fit:.0f}: {why}"] + reasons + ([f"Angle: {angle}"] if angle else [])
        db.x("UPDATE leads SET score=?, fit=?, intent=?, recency=?, reasons=?, updated_at=? WHERE id=?",
             (round(total, 1), round(fit, 1), round(intent, 1), round(recency, 1),
              db.j({"items": all_reasons, "method": method, "angle": angle}), db.now(), lead["id"]))
        done += 1
    return done
