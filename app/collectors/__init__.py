"""Signal collectors. Each takes a play config and returns normalized Signal objects.

Every source here is a public API or feed that permits programmatic access. Nothing logs into
anyone's account or scrapes behind a login.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field


@dataclass
class Signal:
    source: str            # hn | jobs | ats | funding | github | csv
    type: str              # competitor_mention | keyword_intent | hiring | funding | launch | repo_star | import
    title: str
    url: str
    occurred_at: float
    text: str = ""
    company_name: str = ""
    company_domain: str = ""
    person_name: str = ""
    person_handle: str = ""
    person_title: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.type}|{self.url}|{self.title}".encode()).hexdigest()


# How strongly each signal type implies a live need, 0-100.
INTENT_WEIGHT = {
    "competitor_mention": 90, "funding": 75, "hiring": 70, "repo_star": 65,
    "keyword_intent": 60, "launch": 50, "import": 40,
    "competitor_engagement": 85, "keyword_post": 60,
}


def cutoff(cfg: dict) -> float:
    return time.time() - 86400 * float(cfg.get("lookback_days") or 14)


def terms(cfg: dict, key: str) -> list[str]:
    return [t.strip() for t in (cfg.get(key) or []) if isinstance(t, str) and t.strip()]


def matcher(words: list[str]):
    """Case-insensitive whole-phrase matcher; returns the first matching term or ''."""
    if not words:
        return lambda text: ""
    pats = [(w, re.compile(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])")) for w in words]

    def find(text: str) -> str:
        low = (text or "").lower()
        for w, p in pats:
            if p.search(low):
                return w
        return ""
    return find


def registry() -> dict:
    from . import ats, funding, github, hn, jobs
    from .. import linkedin
    return {
        "linkedin": linkedin.collect,
        "hn": hn.collect,
        "jobs": jobs.collect,
        "funding": funding.collect,
        "ats": ats.collect,
        "github": github.collect,
    }


SOURCE_INFO = {
    "linkedin": "LinkedIn (needs a connected provider account): people engaging with the pages and profiles you watch, and authors of posts matching your keywords.",
    "hn": "Hacker News: posts and comments that mention your competitors or pain keywords, plus new launches.",
    "jobs": "Job ads on RemoteOK, Remotive, Arbeitnow and the monthly HN hiring thread that match your hiring-signal titles.",
    "funding": "Funding announcements from TechCrunch and Google News.",
    "ats": "Open roles at your watchlist accounts, read from their Greenhouse, Lever or Ashby job boards.",
    "github": "People who recently starred the GitHub repos you list (competitor or category repos).",
}
