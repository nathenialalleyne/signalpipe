"""Hiring signals from public job feeds. A company advertising for the roles in `hiring_titles` is
telling you what it is about to invest in."""
import asyncio
import html
import re
import time

from .. import web
from . import Signal, cutoff, matcher, terms


def _mk(source: str, company: str, title: str, url: str, ts: float, desc: str, term: str, loc: str = "") -> Signal:
    return Signal(
        source="jobs", type="hiring",
        title=f"{company} is hiring: {title}"[:300],
        text=web.strip_html(html.unescape(desc or ""), 1200),
        url=url, occurred_at=ts, company_name=company.strip(),
        extra={"board": source, "role": title, "term": term, "location": loc},
    )


async def _remoteok(find, since) -> list[Signal]:
    out = []
    for j in (await web.get_json("https://remoteok.com/api"))[1:]:
        term = find(j.get("position", ""))
        ts = float(j.get("epoch") or 0)
        if term and ts >= since and j.get("company"):
            out.append(_mk("remoteok", j["company"], j["position"], j.get("url", ""), ts,
                           j.get("description", ""), term, j.get("location", "")))
    return out


async def _remotive(find, since, words) -> list[Signal]:
    out, seen = [], set()
    for w in words[:6]:
        data = await web.get_json("https://remotive.com/api/remote-jobs", params={"search": w, "limit": 50})
        for j in data.get("jobs", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            term = find(j.get("title", ""))
            try:
                ts = time.mktime(time.strptime(j["publication_date"][:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = time.time()
            if term and ts >= since:
                out.append(_mk("remotive", j["company_name"], j["title"], j["url"], ts,
                               j.get("description", ""), term, j.get("candidate_required_location", "")))
    return out


async def _arbeitnow(find, since) -> list[Signal]:
    out = []
    for page in (1, 2):
        data = await web.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        for j in data.get("data", []):
            term = find(j.get("title", ""))
            ts = float(j.get("created_at") or 0)
            if term and ts >= since:
                out.append(_mk("arbeitnow", j["company_name"], j["title"], j["url"], ts,
                               j.get("description", ""), term, j.get("location", "")))
    return out


async def _hn_hiring(find, since) -> list[Signal]:
    """Top-level comments of the latest 'Ask HN: Who is hiring?' thread. Format: 'Company | Role | Location | ...'."""
    threads = await web.get_json("https://hn.algolia.com/api/v1/search_by_date",
                                 params={"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring",
                                         "hitsPerPage": 1})
    hits = threads.get("hits", [])
    if not hits:
        return []
    story = hits[0]["objectID"]
    out = []
    for page in range(4):
        data = await web.get_json("https://hn.algolia.com/api/v1/search_by_date",
                                  params={"tags": f"comment,story_{story}", "hitsPerPage": 100, "page": page,
                                          "numericFilters": f"created_at_i>{int(since)}"})
        for c in data.get("hits", []):
            if str(c.get("parent_id")) != str(story):
                continue
            raw = c.get("comment_text") or ""
            text = web.strip_html(html.unescape(raw), 1500)
            head = text.split(" | ")
            term = find(text[:600])
            if not term or len(head) < 2:
                continue
            company = re.sub(r"\s*\(.*?\)\s*", " ", head[0]).strip()[:80]
            m = re.search(r'href="(https?://[^"]+)"', html.unescape(raw))
            domain = web.host_of(m.group(1)) if m else ""
            if domain.endswith(("ycombinator.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
                                "linkedin.com", "notion.site", "google.com", "bit.ly", "youtube.com", "youtu.be",
                                "github.com", "twitter.com", "x.com", "medium.com", "substack.com", "calendly.com",
                                "typeform.com", "airtable.com", "wellfound.com", "techcrunch.com", "apple.com",
                                "rippling.com", "gem.com", "breezy.hr", "bamboohr.com", "recruitee.com",
                                "smartrecruiters.com", "workday.com", "teamtailor.com", "personio.de")):
                domain = ""
            s = _mk("hn_whoishiring", company, " | ".join(head[1:3])[:120],
                    f"https://news.ycombinator.com/item?id={c['objectID']}", float(c["created_at_i"]), text, term)
            s.company_domain = domain
            out.append(s)
        if page + 1 >= data.get("nbPages", 1):
            break
    return out


async def collect(cfg: dict) -> list[Signal]:
    words = terms(cfg, "hiring_titles")
    if not words:
        return []
    find, since = matcher(words), cutoff(cfg)
    results = await asyncio.gather(_remoteok(find, since), _remotive(find, since, words), _arbeitnow(find, since),
                                   _hn_hiring(find, since), return_exceptions=True)
    out: list[Signal] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out
