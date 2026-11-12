"""SEO audit of your own site plus a content plan built from the Brain and from what prospects are
actually asking in the signals you collect.

The audit covers what can be measured from outside without paid data: crawlability, on-page basics,
duplication, structured data and keyword coverage. It does not report search volume, rankings or
backlinks; those need a data provider (Search Console is free and is the right next step).
"""
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from . import brain as brain_mod
from . import db, llm, web

SEV = {"high": 3, "medium": 2, "low": 1}
ROLLUP = {
    "http_error": "return an HTTP error", "title_missing": "have no <title>",
    "title_short": "have a title under 25 characters; aim for 30 to 60",
    "title_long": "have a title over 65 characters that will be cut off in results",
    "description_missing": "have no meta description", "description_short": "have a meta description under 70 characters",
    "description_long": "have a meta description that will be truncated", "h1_missing": "have no <h1> heading",
    "h1_multiple": "have more than one <h1> heading", "noindex": "are set to noindex and cannot appear in search",
    "canonical_missing": "have no canonical link", "canonical_offsite": "have a canonical pointing to another domain",
    "alt_missing": "have images without alt text", "thin_content": "have under 250 words of visible text",
    "heading_order": "skip heading levels", "viewport_missing": "have no viewport meta tag",
    "lang_missing": "have no lang attribute on <html>", "og_missing": "are missing an Open Graph title or image",
    "slow": "took over 1.5 seconds to respond", "heavy_html": "have very large HTML", "unreachable": "could not be fetched",
}


def _page_report(url: str, status: int, html: str, ms: int, host: str) -> dict:
    s = web.soup_of(html)
    title = s.title.get_text(strip=True) if s.title else ""
    desc_tag = s.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (desc_tag.get("content") or "").strip() if desc_tag else ""
    canon = s.find("link", rel=lambda v: v and "canonical" in v)
    robots = s.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    h1s = [h.get_text(" ", strip=True) for h in s.find_all("h1")]
    imgs = s.find_all("img")
    internal, external = set(), 0
    for href, _ in web.links(s, url):
        if web.host_of(href) == host:
            internal.add(href.split("#")[0].rstrip("/"))
        elif href.startswith("http"):
            external += 1
    headings = [int(h.name[1]) for h in s.find_all(re.compile("^h[1-6]$"))]
    skipped = any(b - a > 1 for a, b in zip(headings, headings[1:]))
    jsonld = [t for t in s.find_all("script", type="application/ld+json")]
    og = {m.get("property"): m.get("content") for m in s.find_all("meta", property=re.compile("^og:"))}
    html_tag = s.find("html")
    viewport = s.find("meta", attrs={"name": "viewport"})
    text = web.visible_text(s)
    words = len(text.split())
    issues = []

    def add(sev, code, msg):
        issues.append({"severity": sev, "code": code, "message": msg})
    if status >= 400:
        add("high", "http_error", f"Returns HTTP {status}")
    if not title:
        add("high", "title_missing", "No <title>")
    elif len(title) < 25:
        add("medium", "title_short", f"Title is only {len(title)} characters; aim for 30 to 60")
    elif len(title) > 65:
        add("low", "title_long", f"Title is {len(title)} characters and will be cut off in results; aim for under 60")
    if not desc:
        add("medium", "description_missing", "No meta description; Google will pick its own snippet")
    elif len(desc) < 70:
        add("low", "description_short", f"Meta description is {len(desc)} characters; aim for 120 to 160")
    elif len(desc) > 165:
        add("low", "description_long", f"Meta description is {len(desc)} characters and will be truncated")
    if not h1s:
        add("high", "h1_missing", "No <h1> heading")
    elif len(h1s) > 1:
        add("low", "h1_multiple", f"{len(h1s)} <h1> headings; one clear page heading is easier to rank")
    if robots and "noindex" in (robots.get("content") or "").lower():
        add("high", "noindex", "Page is set to noindex and cannot appear in search")
    if not canon:
        add("low", "canonical_missing", "No canonical link")
    elif web.host_of(urljoin(url, canon.get("href", ""))) != host:
        add("medium", "canonical_offsite", "Canonical points to another domain")
    missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())
    if imgs and missing_alt / len(imgs) > 0.3:
        add("low", "alt_missing", f"{missing_alt} of {len(imgs)} images have no alt text")
    if words < 250 and status < 400:
        add("medium", "thin_content", f"Only about {words} words of visible text")
    if skipped:
        add("low", "heading_order", "Heading levels skip (for example h2 straight to h4)")
    if not viewport:
        add("medium", "viewport_missing", "No viewport meta tag; page will not be mobile friendly")
    if not (html_tag and html_tag.get("lang")):
        add("low", "lang_missing", "No lang attribute on <html>")
    if not og.get("og:title") or not og.get("og:image"):
        add("low", "og_missing", "Missing Open Graph title or image; links will preview poorly when shared")
    if ms > 1500:
        add("medium", "slow", f"Server took {ms} ms to respond")
    if len(html) > 1_500_000:
        add("low", "heavy_html", f"HTML is {len(html) // 1024} KB")
    return {"url": url, "status": status, "ms": ms, "title": title, "description": desc, "h1": h1s[:3],
            "words": words, "internal_links": sorted(internal), "external_links": external,
            "structured_data": len(jsonld), "issues": issues, "text": text[:6000]}


async def _sitemap_urls(base: str, robots_txt: str) -> tuple[list[str], str]:
    cands = re.findall(r"(?im)^sitemap:\s*(\S+)", robots_txt) or [base + "/sitemap.xml"]
    urls, found = [], ""
    for sm in cands[:3]:
        try:
            r = await web.client().get(sm, timeout=12)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            found = found or sm
            locs = [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
            if root.tag.endswith("sitemapindex"):
                for child in locs[:3]:
                    try:
                        rc = await web.client().get(child, timeout=12)
                        urls += [e.text.strip() for e in ET.fromstring(rc.content).iter() if e.tag.endswith("loc") and e.text]
                    except Exception:
                        continue
            else:
                urls += locs
        except Exception:
            continue
    return urls, found


async def crawl(url: str, max_pages: int = 30) -> dict:
    if "//" not in url:
        url = "https://" + url
    t0 = time.time()
    first = await web.client().get(url, timeout=20)
    base = f"{urlparse(str(first.url)).scheme}://{urlparse(str(first.url)).netloc}"
    host = web.host_of(base)
    robots_txt = ""
    try:
        rr = await web.client().get(base + "/robots.txt", timeout=10)
        if rr.status_code == 200 and "<html" not in rr.text[:200].lower():
            robots_txt = rr.text
    except Exception:
        pass
    sm_urls, sm_found = await _sitemap_urls(base, robots_txt)
    llms = False
    try:
        lr = await web.client().get(base + "/llms.txt", timeout=8)
        llms = lr.status_code == 200 and "<html" not in lr.text[:200].lower()
    except Exception:
        pass

    queue = [str(first.url).split("#")[0].rstrip("/")]
    seen = set(queue)
    pages: list[dict] = []
    sem = asyncio.Semaphore(4)

    async def fetch(u: str):
        async with sem:
            t = time.time()
            try:
                r = await web.client().get(u, timeout=20)
            except Exception:
                return {"url": u, "status": 599, "ms": 0, "title": "", "description": "", "h1": [], "words": 0,
                        "internal_links": [], "external_links": 0, "structured_data": 0, "text": "",
                        "issues": [{"severity": "high", "code": "unreachable", "message": "Could not be fetched"}]}
            if "html" not in r.headers.get("content-type", "html"):
                return None
            return _page_report(str(r.url).split("#")[0].rstrip("/"), r.status_code, r.text,
                                int((time.time() - t) * 1000), host)

    skip = re.compile(r"\.(pdf|png|jpe?g|gif|svg|webp|zip|mp4|css|js|xml|ico)$|/(wp-admin|cdn-cgi|login|signin|signup|cart)\b", re.I)
    while queue and len(pages) < max_pages:
        batch, queue = queue[: max_pages - len(pages)][:8], queue[8:]
        for rep in await asyncio.gather(*[fetch(u) for u in batch]):
            if rep is None:
                continue
            pages.append(rep)
            for l in rep["internal_links"]:
                if l not in seen and not skip.search(l) and len(seen) < max_pages * 4:
                    seen.add(l)
                    queue.append(l)
        if len(pages) < max_pages and not queue:
            extra = [u.rstrip("/") for u in sm_urls if web.host_of(u) == host and u.rstrip("/") not in seen][: max_pages - len(pages)]
            seen.update(extra)
            queue.extend(extra)
    return {"base": base, "host": host, "pages": pages, "robots_txt": bool(robots_txt),
            "robots_blocks_all": bool(re.search(r"(?im)^user-agent:\s*\*\s*\n(?:.*\n)*?disallow:\s*/\s*$", robots_txt)),
            "sitemap": sm_found, "sitemap_urls": len(sm_urls), "llms_txt": llms, "https": base.startswith("https"),
            "seconds": round(time.time() - t0, 1)}


def site_issues(c: dict) -> list[dict]:
    out = []

    def add(sev, code, msg, urls=None):
        out.append({"severity": sev, "code": code, "message": msg, "urls": (urls or [])[:8]})
    if not c["https"]:
        add("high", "no_https", "Site is not served over HTTPS")
    if c["robots_blocks_all"]:
        add("high", "robots_block", "robots.txt disallows the whole site for all crawlers")
    if not c["robots_txt"]:
        add("low", "robots_missing", "No robots.txt")
    if not c["sitemap"]:
        add("medium", "sitemap_missing", "No XML sitemap found; add one and reference it from robots.txt")
    if not c["llms_txt"]:
        add("low", "llms_txt_missing", "No /llms.txt; a short one helps AI assistants describe and cite you correctly")
    pages = [p for p in c["pages"] if p["status"] < 400]
    for field, label in (("title", "title"), ("description", "meta description")):
        groups: dict[str, list[str]] = {}
        for p in pages:
            if p[field]:
                groups.setdefault(p[field].lower(), []).append(p["url"])
        dups = [u for g in groups.values() if len(g) > 1 for u in g]
        if dups:
            add("medium", f"duplicate_{field}", f"{len(dups)} pages share a {label} with another page", dups)
    broken = [p["url"] for p in c["pages"] if p["status"] >= 400]
    if broken:
        add("high", "broken_pages", f"{len(broken)} internally linked pages return errors", broken)
    crawled = {p["url"] for p in pages}
    inbound: dict[str, int] = {u: 0 for u in crawled}
    for p in pages:
        for l in p["internal_links"]:
            if l in inbound and l != p["url"]:
                inbound[l] += 1
    orphans = [u for u, n in inbound.items() if n == 0 and u != pages[0]["url"]] if pages else []
    if orphans:
        add("low", "orphans", f"{len(orphans)} pages are in the sitemap but nothing links to them", orphans)
    if pages and not any(p["structured_data"] for p in pages):
        add("medium", "no_structured_data", "No schema.org structured data anywhere; add Organization and Product or FAQ markup")
    # Roll page-level issues up by type.
    by_code: dict[str, list] = {}
    for p in c["pages"]:
        for i in p["issues"]:
            by_code.setdefault(i["code"], []).append((p["url"], i))
    for code, items in by_code.items():
        sev = items[0][1]["severity"]
        msg = items[0][1]["message"] if len(items) == 1 else f"{len(items)} pages: {ROLLUP.get(code, items[0][1]['message'])}"
        add(sev, code, msg, [u for u, _ in items])
    out.sort(key=lambda i: -SEV[i["severity"]])
    return out


def score(issues: list[dict], n_pages: int) -> int:
    penalty = 0.0
    for i in issues:
        share = min(1.0, max(1, len(i["urls"])) / max(1, n_pages))
        penalty += {"high": 14, "medium": 6, "low": 2}[i["severity"]] * (0.4 + 0.6 * share)
    return max(0, round(100 - penalty))


def keyword_coverage(c: dict, brain: dict) -> list[dict]:
    """For each target phrase: is there a page that is clearly about it (title or h1), or only mentions it?"""
    targets = list(dict.fromkeys(
        [k for k in brain.get("keywords", [])] +
        [f"{x} alternative" for x in brain.get("competitors", [])[:6]] +
        [f"{(brain.get('keywords') or ['software'])[0]} for {i}" for i in brain["icp"].get("industries", [])[:3]]))
    out = []
    for t in targets[:25]:
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])")
        focus = [p["url"] for p in c["pages"] if rx.search(f"{p['title']} {' '.join(p['h1'])}".lower())]
        mention = [p["url"] for p in c["pages"] if rx.search(p["text"].lower())]
        status = "covered" if focus else ("mentioned" if mention else "missing")
        out.append({"keyword": t, "status": status, "pages": (focus or mention)[:3]})
    return out


PLAN_SCHEMA = llm.obj({"briefs": llm.arr(llm.obj({
    "title": llm.STR, "target_keyword": llm.STR,
    "intent": {"type": "string", "enum": ["comparison", "problem", "how_to", "use_case", "pricing", "definition"]},
    "why": llm.STR, "outline": llm.STRS, "internal_link_to": llm.STR,
}))})


async def content_plan(c: dict, coverage: list[dict], brain: dict) -> tuple[list[dict], str]:
    questions = [r["title"] for r in db.q(
        "SELECT title FROM signals WHERE source='hn' AND type IN ('competitor_mention','keyword_intent') "
        "ORDER BY occurred_at DESC LIMIT 25")]
    gaps = [k for k in coverage if k["status"] != "covered"]
    if llm.available():
        prompt = (
            "# Existing pages\n" + "\n".join(f"- {p['url']} | {p['title']}" for p in c["pages"][:40]) +
            "\n\n# Target phrases with no dedicated page\n" + "\n".join(f"- {g['keyword']} ({g['status']})" for g in gaps) +
            "\n\n# What prospects are asking in public right now\n" + "\n".join(f"- {q}" for q in questions))
        data = await llm.ask_json(
            "You plan search content for a small B2B company whose founder writes it personally, so every piece has "
            "to earn its place. From the gaps and the real prospect questions below, propose 8 pages that would bring "
            "in buyers, not just traffic: favour comparison, alternative-to, problem and use-case pages over generic "
            "definitions. Each brief: a specific title a searcher would click, the one phrase it targets, why this "
            "page and why now (cite the gap or the question it answers), a 4 to 6 point outline with the concrete "
            "content of each section, and which existing page it should link to. Do not propose pages that duplicate "
            "an existing one.\n\n# Company\n" + brain_mod.summary(brain), prompt, PLAN_SCHEMA, effort="medium", max_tokens=8000)
        if data and data.get("briefs"):
            return data["briefs"], "claude"
    briefs = []
    for g in gaps[:8]:
        k = g["keyword"]
        if k.endswith(" alternative"):
            comp = k[: -len(" alternative")]
            briefs.append({"title": f"{comp} alternative: an honest comparison with {brain.get('company') or 'us'}",
                           "target_keyword": k, "intent": "comparison",
                           "why": f"Buyers comparing against {comp} search this and you have no page for it.",
                           "outline": [f"Who {comp} is right for", "Where teams outgrow it", "Side-by-side: pricing, setup, limits",
                                       "How to switch", "FAQ"], "internal_link_to": c["base"]})
        else:
            briefs.append({"title": f"{k.title()}: what it takes and how to get it right", "target_keyword": k, "intent": "problem",
                           "why": "Target phrase with no dedicated page.",
                           "outline": ["The problem in the buyer's words", "What most teams try first", "A better approach",
                                       "Worked example", "FAQ"], "internal_link_to": c["base"]})
    return briefs, "template"


async def audit(url: str, max_pages: int = 30) -> dict:
    c = await crawl(url, max_pages)
    brain = brain_mod.get()
    issues = site_issues(c)
    coverage = keyword_coverage(c, brain)
    plan, plan_source = await content_plan(c, coverage, brain)
    report = {
        "url": c["base"], "score": score(issues, len(c["pages"])), "pages_crawled": len(c["pages"]),
        "seconds": c["seconds"], "sitemap": c["sitemap"], "sitemap_urls": c["sitemap_urls"],
        "robots_txt": c["robots_txt"], "llms_txt": c["llms_txt"], "issues": issues, "coverage": coverage,
        "content_plan": plan, "plan_source": plan_source,
        "pages": [{k: p[k] for k in ("url", "status", "ms", "title", "words", "structured_data")} |
                  {"issue_count": len(p["issues"])} for p in c["pages"]],
    }
    db.x("INSERT INTO seo_audits(url,created_at,report) VALUES(?,?,?)", (c["base"], db.now(), db.j(report)))
    return report
