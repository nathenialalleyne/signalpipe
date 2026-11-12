"""The Brain: what you sell, who buys it, and how you talk about it. Built from your website, then editable."""
import re
from urllib.parse import urlparse

from . import db, llm, web

EMPTY = {
    "company": "", "website": "", "one_liner": "", "offer": "", "value_props": [], "proof_points": [],
    "icp": {"industries": [], "company_sizes": [], "titles": [], "geos": [], "disqualifiers": []},
    "pains": [], "competitors": [], "keywords": [], "hiring_signals": [], "objections": [],
    "tone": "direct, specific, no hype", "sender_name": "", "sender_title": "", "calendar_link": "",
}

SCHEMA = llm.obj({
    "company": llm.STR, "one_liner": llm.STR, "offer": llm.STR,
    "value_props": llm.STRS, "proof_points": llm.STRS,
    "icp": llm.obj({"industries": llm.STRS, "company_sizes": llm.STRS, "titles": llm.STRS,
                    "geos": llm.STRS, "disqualifiers": llm.STRS}),
    "pains": llm.STRS, "competitors": llm.STRS, "keywords": llm.STRS,
    "hiring_signals": llm.STRS, "objections": llm.STRS, "tone": llm.STR,
})

SYSTEM = """You are setting up an outbound sales system for a company, working only from its website text.
Produce the profile a seasoned founder-led sales team would write for itself.

Field guidance:
- one_liner: what the company does, in plain words, under 25 words.
- offer: the product or service and how it is bought.
- value_props / proof_points: only what the site actually supports (customers, numbers, outcomes). Leave proof_points empty rather than invent any.
- icp.titles: the job titles of the people who buy or champion this, most likely first.
- icp.company_sizes: short ranges such as "11-50 employees".
- pains: the problems a buyer feels before they go looking, phrased the way they would say them.
- competitors: named alternatives a buyer would compare against, including the status-quo tools.
- keywords: 8 to 15 short phrases a prospect would type or say in public when they have this problem. These drive search, so prefer 1 to 3 word phrases that really occur in forum posts and job ads over marketing language.
- hiring_signals: job titles whose appearance in a company's job ads suggests that company now needs this product.
- objections: the pushback a salesperson hears most.
- tone: how the site's own copy sounds, in a few words."""


async def crawl_site(url: str, max_pages: int = 6) -> tuple[str, str]:
    """Homepage plus the most informative internal pages, as text."""
    if "//" not in url:
        url = "https://" + url
    home = await web.fetch_page(url, respect_robots=False)
    if home is None:
        raise ValueError(f"Could not load {url}")
    base = str(home.url)
    host = web.host_of(base)
    soup = web.soup_of(home.text)
    wanted = re.compile(r"(about|product|pricing|solution|customer|case|feature|platform|service|use-case|why)", re.I)
    seen, picks = {base.rstrip("/")}, []
    for href, _ in web.links(soup, base):
        clean = href.split("#")[0].rstrip("/")
        if web.host_of(clean) == host and clean not in seen and wanted.search(urlparse(clean).path):
            seen.add(clean)
            picks.append(clean)
    chunks = [f"# {base}\n{web.visible_text(soup, 6000)}"]
    for p in sorted(picks, key=lambda u: len(urlparse(u).path))[: max_pages - 1]:
        r = await web.fetch_page(p, respect_robots=False)
        if r is not None and r.status_code == 200:
            chunks.append(f"# {p}\n{web.strip_html(r.text, 4000)}")
    return base, "\n\n".join(chunks)


def _heuristic(base: str, text: str) -> dict:
    """No-LLM fallback: fill what can be read directly off the page and leave the rest for the user."""
    host = web.host_of(base)
    first = text.split("\n", 2)[1] if "\n" in text else text
    words = re.findall(r"[a-z][a-z\-]{3,}", text.lower())
    stop = set("this that with from your have will more about their what when which they them into than then been "
               "were also over just like make made does used using only very most some such each other those these "
               "here there where while after before company product products customers learn sign login contact "
               "pricing privacy terms cookies rights reserved free trial demo started book blog careers".split())
    freq: dict[str, int] = {}
    for a, b in zip(words, words[1:]):
        if a not in stop and b not in stop:
            freq[f"{a} {b}"] = freq.get(f"{a} {b}", 0) + 1
    keywords = [k for k, v in sorted(freq.items(), key=lambda kv: -kv[1]) if v > 1][:12]
    out = dict(EMPTY)
    out.update(company=host.split(".")[0].title(), one_liner=first[:180], keywords=keywords)
    return out


async def build(url: str) -> dict:
    base, text = await crawl_site(url)
    data = await llm.ask_json(SYSTEM, f"Website: {base}\n\n{text}", SCHEMA, effort="medium", max_tokens=6000)
    source = "llm"
    if data is None:
        data, source = _heuristic(base, text), "heuristic"
    prev = get()
    brain = {**EMPTY, **data}
    brain["website"] = base
    for keep in ("sender_name", "sender_title", "calendar_link"):
        brain[keep] = prev.get(keep, "")
    brain["_source"] = source
    db.set_setting("brain", brain)
    return brain


def get() -> dict:
    b = db.get_setting("brain") or {}
    merged = {**EMPTY, **b}
    merged["icp"] = {**EMPTY["icp"], **(b.get("icp") or {})}
    return merged


def save(patch: dict) -> dict:
    b = get()
    for k, v in patch.items():
        if k == "icp" and isinstance(v, dict):
            b["icp"] = {**b["icp"], **v}
        elif k in EMPTY:
            b[k] = v
    db.set_setting("brain", b)
    return b


def suggested_play(brain: dict) -> dict:
    """A starting play config derived from the Brain."""
    return {
        "keywords": brain.get("keywords", [])[:10],
        "competitors": brain.get("competitors", [])[:8],
        "hiring_titles": brain.get("hiring_signals", [])[:8],
        "funding_keywords": (brain.get("icp", {}).get("industries") or [])[:6],
        "watchlist": [], "github_repos": [],
        "sources": ["hn", "jobs", "funding"],
        "lookback_days": 14, "min_score": 45,
        "channels": ["email", "linkedin", "phone"],
    }


def summary(brain: dict, limit: int = 1800) -> str:
    """Compact text form used inside prompts."""
    icp = brain.get("icp", {})
    lines = [
        f"Company: {brain.get('company')} ({brain.get('website')})",
        f"What we do: {brain.get('one_liner')}",
        f"Offer: {brain.get('offer')}",
        f"Value props: {'; '.join(brain.get('value_props', [])[:5])}",
        f"Proof points: {'; '.join(brain.get('proof_points', [])[:4])}",
        f"ICP industries: {', '.join(icp.get('industries', []))}",
        f"ICP company sizes: {', '.join(icp.get('company_sizes', []))}",
        f"ICP buyer titles: {', '.join(icp.get('titles', []))}",
        f"ICP geos: {', '.join(icp.get('geos', []))}",
        f"Disqualifiers: {', '.join(icp.get('disqualifiers', []))}",
        f"Pains we solve: {'; '.join(brain.get('pains', [])[:6])}",
        f"Competitors: {', '.join(brain.get('competitors', []))}",
    ]
    return "\n".join(l for l in lines if not l.endswith(": "))[:limit]
