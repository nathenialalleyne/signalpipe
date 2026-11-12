"""Hacker News via the public Algolia API: competitor mentions, pain-keyword posts, launches."""
import re

from .. import web
from . import Signal, cutoff, terms

API = "https://hn.algolia.com/api/v1/search_by_date"
ASK = re.compile(r"\b(alternative|alternatives|recommend|looking for|switch(ing)? from|replace|migrat|vs\.?|instead of|"
                 r"anyone us(e|ing)|what do you use|frustrat|too expensive|pricing)\b", re.I)


async def _search(query: str, since: float, tags: str, n: int = 30) -> list[dict]:
    try:
        data = await web.get_json(API, params={
            "query": f'"{query}"', "tags": tags, "hitsPerPage": n,
            "numericFilters": f"created_at_i>{int(since)}",
        })
        return data.get("hits", [])
    except Exception:
        return []


def _signal(hit: dict, kind: str, term: str) -> Signal:
    is_comment = "comment" in hit.get("_tags", [])
    title = hit.get("title") or hit.get("story_title") or ""
    body = web.strip_html(hit.get("comment_text") or hit.get("story_text") or "", 1500)
    ext = hit.get("url") or ""
    domain = ""
    show = "show_hn" in hit.get("_tags", []) or title.lower().startswith(("show hn", "launch hn"))
    if ext and show:
        h = web.host_of(ext)
        if h and not h.endswith(("github.com", "github.io", "youtube.com", "medium.com", "substack.com",
                                 "apple.com", "google.com", "twitter.com", "x.com", "npmjs.com")):
            domain = h
    return Signal(
        source="hn", type=kind,
        title=(f"Comment on: {title}" if is_comment else title)[:300],
        text=body,
        url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
        occurred_at=float(hit.get("created_at_i") or 0),
        company_domain=domain,
        person_handle=f"hn:{hit.get('author', '')}",
        extra={"term": term, "external_url": ext, "asks": bool(ASK.search(f"{title} {body}")),
               "points": hit.get("points")},
    )


NOISE = re.compile(r"who wants to be hired|who is hiring|freelancer\? seeking", re.I)


async def collect(cfg: dict) -> list[Signal]:
    since = cutoff(cfg)
    out: list[Signal] = []
    out = await _collect(cfg, since)
    return [s for s in out if not NOISE.search(s.title)]


async def _collect(cfg: dict, since: float) -> list[Signal]:
    out: list[Signal] = []
    for comp in terms(cfg, "competitors")[:8]:
        for hit in await _search(comp, since, "(story,comment)"):
            s = _signal(hit, "competitor_mention", comp)
            # A bare mention is weak; keep it only when the author is weighing or leaving the competitor.
            if s.extra["asks"]:
                out.append(s)
    for kw in terms(cfg, "keywords")[:10]:
        for hit in await _search(kw, since, "(story,comment)", n=20):
            s = _signal(hit, "keyword_intent", kw)
            if s.extra["asks"] or hit.get("title", "").lower().startswith("ask hn"):
                out.append(s)
            elif s.company_domain:
                s.type = "launch"
                out.append(s)
    return out
