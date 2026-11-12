"""Open roles at named target accounts, read from their public Greenhouse, Lever or Ashby job boards.

Watchlist entries are domains or bare company slugs, e.g. "ramp.com" or "ramp".
"""
import asyncio
import datetime as dt
import re

from .. import db, web
from . import Signal, cutoff, matcher, terms


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


async def _greenhouse(slug: str):
    data = await web.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [(j["title"], j["absolute_url"], _ts(j.get("first_published") or j.get("updated_at")),
             (j.get("location") or {}).get("name", "")) for j in data.get("jobs", [])]


async def _lever(slug: str):
    data = await web.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    return [(j["text"], j["hostedUrl"], _ts(j.get("createdAt")), (j.get("categories") or {}).get("location", ""))
            for j in data]


async def _ashby(slug: str):
    data = await web.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [(j["title"], j["jobUrl"], _ts(j.get("publishedAt")), j.get("location", "")) for j in data.get("jobs", [])]


BOARDS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


async def _board_for(slug: str):
    """Find which ATS hosts this company; remembered for 30 days."""
    known = db.cache_get(f"ats:{slug}", 30 * 86400)
    order = [known] if known in BOARDS else list(BOARDS)
    for name in order:
        try:
            jobs = await BOARDS[name](slug)
            if jobs:
                db.cache_set(f"ats:{slug}", name)
                return name, jobs
        except Exception:
            continue
    return None, []


async def collect(cfg: dict) -> list[Signal]:
    watch = terms(cfg, "watchlist")[:60]
    if not watch:
        return []
    find = matcher(terms(cfg, "hiring_titles"))
    since = cutoff(cfg)
    sem = asyncio.Semaphore(6)

    async def one(entry: str) -> list[Signal]:
        domain = web.host_of(entry) if "." in entry else ""
        slug = re.sub(r"[^a-z0-9]", "", (domain.split(".")[0] if domain else entry).lower())
        async with sem:
            board, jobs = await _board_for(slug)
        out = []
        for title, url, ts, loc in jobs:
            term = find(title)
            if term and ts >= since:
                out.append(Signal(
                    source="ats", type="hiring", title=f"{slug.title()} is hiring: {title}"[:300], url=url,
                    occurred_at=ts, company_name=slug.title(), company_domain=domain,
                    extra={"board": board, "role": title, "term": term, "location": loc, "watchlist": True},
                ))
        return out

    results = await asyncio.gather(*[one(w) for w in watch], return_exceptions=True)
    return [s for r in results if isinstance(r, list) for s in r]
