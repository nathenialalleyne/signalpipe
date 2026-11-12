"""What is working: automatic A/B testing of opening angles, and outcome tracking by segment.

Every approved lead is assigned one of a few opening angles. Which angle a new lead gets is decided by
Thompson sampling over past results, so angles that earn replies are used more often while the others
still get enough traffic to prove themselves. Results are also broken down by signal, buyer title,
score band and channel, so you can see where your real customers come from.
"""
import random
import re

from . import db

# The opening strategy for the first email and the LinkedIn note. Wording differs; facts never do.
ANGLES = {
    "signal": "Open with the signal itself, stated plainly, then connect it to the problem.",
    "pain": "Open with the problem in the words a buyer would use, then give the signal as the reason you thought of them.",
    "question": "Open with one specific question about how they handle the problem today; bring in the signal in the second sentence.",
    "proof": "Open with one concrete result from the company profile's proof points, then the signal. If the profile has no "
             "real proof point, open with the signal instead and do not invent one.",
}
ANGLE_LABEL = {"signal": "Leads with the signal", "pain": "Leads with the pain", "question": "Leads with a question",
               "proof": "Leads with proof"}
REACHED = ("contacted", "replied", "won", "lost")
POSITIVE = ("reply:interested", "reply:question", "reply:referral")
MIN_JUDGE = 10  # below this many contacted leads a rate is noise, and the UI says so


def _outcomes(play_id: int | None = None) -> list[dict]:
    """One row per lead that was actually contacted, with what happened next."""
    where, params = "l.status IN (?,?,?,?)", list(REACHED)
    if play_id:
        where += " AND l.play_id=?"
        params.append(play_id)
    rows = db.q(
        f"SELECT l.id, l.play_id, l.status, l.score, l.variant, "
        f"(SELECT s.type FROM signals s WHERE s.lead_id=l.id ORDER BY occurred_at DESC LIMIT 1) signal, "
        f"(SELECT c.title FROM contacts c WHERE c.lead_id=l.id ORDER BY is_primary DESC, id LIMIT 1) title, "
        f"(SELECT COUNT(*) FROM messages m WHERE m.lead_id=l.id AND m.channel='linkedin' AND m.outcome='accepted') li_accepted, "
        f"(SELECT COUNT(*) FROM messages m WHERE m.lead_id=l.id AND m.channel='linkedin' AND m.step=1 AND m.status='done') li_invited, "
        f"(SELECT COUNT(*) FROM messages m WHERE m.lead_id=l.id AND m.channel='email' AND m.status='sent') emails, "
        f"(SELECT COUNT(*) FROM messages m WHERE m.lead_id=l.id AND m.channel='call' AND m.status='done') calls, "
        f"(SELECT COUNT(*) FROM replies r WHERE r.lead_id=l.id AND r.kind LIKE 'reply%') replies, "
        f"(SELECT COUNT(*) FROM replies r WHERE r.lead_id=l.id AND r.kind IN (?,?,?)) positive "
        f"FROM leads l WHERE {where}", [*POSITIVE, *params])
    for r in rows:
        r["replied"] = bool(r["replies"]) or r["status"] in ("replied", "won")
        r["positive"] = bool(r["positive"]) or r["status"] == "won"
        r["won"] = r["status"] == "won"
        r["accepted"] = bool(r["li_accepted"])
        # Something came back: a reply, or at least an accepted connection.
        r["engaged"] = r["replied"] or r["accepted"]
    return rows


def title_bucket(title: str | None) -> str:
    t = (title or "").lower()
    if re.search(r"founder|ceo|chief executive|owner|president", t):
        return "Founder or CEO"
    if re.search(r"chief|cro|cmo|coo|cto|cfo|vp|vice president|head of", t):
        return "Executive or VP"
    if re.search(r"director|manager|lead\b", t):
        return "Director or manager"
    return "Other or unknown"


def score_band(score: float) -> str:
    return "Score 65 and up" if score >= 65 else "Score 45 to 64" if score >= 45 else "Score under 45"


def pick_angle(play_id: int | None = None, rng: random.Random | None = None) -> str:
    """Thompson sampling: draw each angle's plausible engagement rate from what we have seen, use the best draw."""
    rng = rng or random
    stats = {a: [0, 0] for a in ANGLES}
    for r in _outcomes(play_id):
        if r["variant"] in stats:
            stats[r["variant"]][0] += 1
            stats[r["variant"]][1] += 1 if r["engaged"] else 0
    draws = {a: rng.betavariate(1 + won, 1 + n - won) for a, (n, won) in stats.items()}
    return max(draws, key=draws.get)


def _table(rows: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r) or "Unknown", []).append(r)
    out = []
    for name, rs in groups.items():
        n = len(rs)
        invited = sum(1 for r in rs if r["li_invited"])
        out.append({
            "name": name, "contacted": n,
            "accepted": sum(r["accepted"] for r in rs), "invited": invited,
            "replied": sum(r["replied"] for r in rs), "positive": sum(r["positive"] for r in rs),
            "won": sum(r["won"] for r in rs),
            "reply_rate": round(100 * sum(r["replied"] for r in rs) / n, 1),
            "accept_rate": round(100 * sum(r["accepted"] for r in rs) / invited, 1) if invited else None,
            "enough": n >= MIN_JUDGE,
        })
    return sorted(out, key=lambda g: (-g["enough"], -g["reply_rate"], -g["contacted"]))


def _leader(table: list[dict], what: str) -> str | None:
    judged = [g for g in table if g["enough"]]
    if len(judged) < 2 or judged[0]["reply_rate"] == judged[-1]["reply_rate"]:
        return None
    best, worst = judged[0], judged[-1]
    return (f"{what}: \"{best['name']}\" gets replies from {best['reply_rate']:g}% of leads "
            f"({best['replied']} of {best['contacted']}), against {worst['reply_rate']:g}% for \"{worst['name']}\".")


def report(play_id: int | None = None) -> dict:
    rows = _outcomes(play_id)
    where, params = ("WHERE play_id=?", [play_id]) if play_id else ("", [])
    counts = {r["status"]: r["n"] for r in db.q(f"SELECT status, COUNT(*) n FROM leads {where} GROUP BY status", params)}
    reviewed = sum(counts.get(s, 0) for s in ("approved", "contacted", "replied", "won", "lost", "rejected"))
    approved = reviewed - counts.get("rejected", 0)
    tables = {
        "angle": _table(rows, lambda r: ANGLE_LABEL.get(r["variant"], "Before A/B testing")),
        "signal": _table(rows, lambda r: (r["signal"] or "unknown").replace("_", " ").capitalize()),
        "title": _table(rows, lambda r: title_bucket(r["title"])),
        "score": _table(rows, lambda r: score_band(r["score"])),
        "channel": _table(rows, lambda r: " + ".join(c for c, used in (("Email", r["emails"]), ("LinkedIn", r["li_invited"]),
                                                                     ("Call", r["calls"])) if used) or "None sent yet"),
    }
    findings = [f for f in (
        _leader(tables["angle"], "Best opening"), _leader(tables["signal"], "Best signal"),
        _leader(tables["title"], "Best buyer level"), _leader(tables["score"], "Score check")) if f]
    n = len(rows)
    return {
        "funnel": {
            "found": sum(counts.values()), "reviewed": reviewed, "approved": approved,
            "approval_rate": round(100 * approved / reviewed, 1) if reviewed else None,
            "contacted": n, "accepted": sum(r["accepted"] for r in rows), "replied": sum(r["replied"] for r in rows),
            "positive": sum(r["positive"] for r in rows), "won": sum(r["won"] for r in rows),
            "reply_rate": round(100 * sum(r["replied"] for r in rows) / n, 1) if n else None,
        },
        "tables": tables, "findings": findings, "min_judge": MIN_JUDGE,
        "enough_data": n >= 2 * MIN_JUDGE,
    }
