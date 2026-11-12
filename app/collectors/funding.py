"""Funding announcements from news feeds. Fresh capital means budget and a mandate to grow."""
import email.utils
import html
import re
import xml.etree.ElementTree as ET

from .. import web
from . import Signal, cutoff, matcher, terms

VERBS = (r"raises|raised|secures|secured|lands|closes|closed|nabs|bags|snags|gets|announces|completes|"
         r"pulls in|hauls in|picks up|scores")
# "<who> raises $12M ..." where <who> may carry a descriptor: "Fintech startup Coast", "Acme, a payments firm,"
HEADLINE = re.compile(
    rf"^(?:exclusive:\s*)?(?P<pre>.{{2,110}}?)\s+(?:{VERBS})\s+(?:an?\s+|another\s+|over\s+|nearly\s+)?"
    r"(?P<amt>(?:US)?[$€£]\s?\d[\d.,]*\s?(?:million|billion|mn|bn|m|b|k)?)\b", re.I)
ROUND = re.compile(r"\b(pre-seed|seed|series [a-f]|growth round|bridge round)\b", re.I)
NOT_COMPANY = re.compile(r"\b(ventures|capital|partners|fund|vc|investors?|management)\b", re.I)
IS_FUND = re.compile(r"\b(fund|funds|vehicle|ipo|acquisition|acquires|debt facility)\b", re.I)
DESCRIPTOR = re.compile(r"^.*\b(startup|start-up|company|firm|platform|maker|developer|provider|unicorn|"
                        r"specialist|lab|studio|app|challenger|scaleup|scale-up|rival|competitor)\s+(?=[A-Z0-9])", re.I)


def _items(xml: str):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "")
        link = it.findtext("link") or ""
        date = it.findtext("pubDate") or ""
        desc = web.strip_html(html.unescape(it.findtext("description") or ""), 600)
        try:
            ts = email.utils.parsedate_to_datetime(date).timestamp()
        except Exception:
            ts = 0.0
        yield title, link, ts, desc


def parse_headline(title: str) -> dict | None:
    title = re.sub(r"\s+[-|–]\s+[^-|–]{2,40}$", "", title.strip())  # drop trailing " - Publisher"
    m = HEADLINE.match(title)
    if not m or IS_FUND.search(title[m.end("amt"):][:60]) or IS_FUND.search(m.group("pre")):
        return None
    pre = m.group("pre").strip()
    company = pre.split(",")[0].strip()          # "Acme, a payments firm," -> "Acme"
    company = DESCRIPTOR.sub("", company).strip()  # "Fintech startup Coast" -> "Coast"
    company = re.sub(r"[’']s$", "", company).strip(" ,:")
    if (not company or NOT_COMPANY.search(company) or len(company.split()) > 3
            or not re.match(r"[A-Z0-9]", company) or "’s " in company or "'s " in company):
        return None
    rnd = ROUND.search(title)
    return {"company": company, "amount": m.group("amt").strip(), "round": rnd.group(1).title() if rnd else ""}


async def collect(cfg: dict) -> list[Signal]:
    since = cutoff(cfg)
    focus = terms(cfg, "funding_keywords")
    find = matcher(focus)
    days = max(1, int(cfg.get("lookback_days") or 14))
    feeds = [("techcrunch", "https://techcrunch.com/category/venture/feed/", {})]
    queries = [f'"{f}" raises funding round' for f in focus[:6]] or ["startup raises series A", "raises seed round"]
    for qy in queries:
        feeds.append(("google_news", "https://news.google.com/rss/search",
                      {"q": f"{qy} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    out, seen = [], set()
    for name, url, params in feeds:
        try:
            xml = await web.get_text(url, params=params)
        except Exception:
            continue
        for title, link, ts, desc in _items(xml):
            if ts < since:
                continue
            info = parse_headline(title)
            if not info or info["company"].lower() in seen:
                continue
            term = find(f"{title} {desc}")
            if focus and not term and name == "techcrunch":
                continue  # unfiltered feed: keep only on-topic rounds when a focus is set
            seen.add(info["company"].lower())
            out.append(Signal(
                source="funding", type="funding", title=title[:300], text=desc, url=link, occurred_at=ts,
                company_name=info["company"],
                extra={"amount": info["amount"], "round": info["round"], "feed": name, "term": term},
            ))
    return out
