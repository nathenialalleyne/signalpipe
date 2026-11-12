"""Turn a company name into something you can act on: domain, description, tech stack, people,
emails, phone numbers and LinkedIn URLs. Everything comes from the company's own public website,
plus optional paid lookups when a key is configured.
"""
import asyncio
import json
import re
from urllib.parse import urlparse

from . import brain as brain_mod
from . import config, db, llm, validate, web

TECH = {
    "HubSpot": r"js\.hs-scripts\.com|js\.hsforms\.net|hs-analytics|hubspot\.com",
    "Salesforce": r"pardot\.com|force\.com|salesforce\.com/|sfdcstatic",
    "Marketo": r"munchkin\.js|marketo\.(com|net)",
    "Intercom": r"widget\.intercom\.io|intercomSettings|intercomcdn",
    "Drift": r"js\.driftt\.com|drift\.com/",
    "Zendesk": r"zdassets\.com|zendesk\.com",
    "Crisp": r"client\.crisp\.chat",
    "Segment": r"cdn\.segment\.com|analytics\.js/v1",
    "Google Tag Manager": r"googletagmanager\.com",
    "Mixpanel": r"cdn\.mxpnl\.com|mixpanel\.com",
    "Amplitude": r"cdn\.amplitude\.com|amplitude\.com/libs",
    "PostHog": r"posthog\.com|/ingest/static/array\.js",
    "Hotjar": r"static\.hotjar\.com",
    "Clearbit": r"tag\.clearbitscripts\.com",
    "6sense": r"6sc\.co|6sense\.com",
    "Apollo": r"assets\.apollo\.io",
    "Outreach": r"outreach\.io",
    "Calendly": r"calendly\.com",
    "Chili Piper": r"chilipiper\.com",
    "Typeform": r"typeform\.com",
    "Klaviyo": r"klaviyo\.com",
    "Mailchimp": r"chimpstatic\.com|list-manage\.com",
    "ActiveCampaign": r"activehosted\.com|trackcmp\.net",
    "Pipedrive": r"pipedrive\.com|leadbooster",
    "Stripe": r"js\.stripe\.com",
    "Shopify": r"cdn\.shopify\.com",
    "WordPress": r"/wp-content/|/wp-includes/",
    "Webflow": r"assets\.website-files\.com|webflow\.com",
    "Framer": r"framerusercontent\.com",
    "Next.js": r"/_next/static/",
    "Cloudflare": r"cdnjs\.cloudflare\.com|cf-beacon",
    "Gong": r"gong\.io",
    "LinkedIn Insight": r"snap\.licdn\.com",
    "Meta Pixel": r"connect\.facebook\.net",
    "G2": r"tracking\.g2crowd\.com",
    "Qualified": r"js\.qualified\.com",
    "RB2B": r"rb2b\.com",
}
TECH_RE = {k: re.compile(v, re.I) for k, v in TECH.items()}

PAGE_HINT = re.compile(r"/(about|team|people|leadership|company|contact|contact-us|our-team|who-we-are|management|"
                       r"founders|impressum|imprint)(/|$|\.|-us)", re.I)
TITLE_WORDS = re.compile(r"\b(ceo|cto|coo|cfo|cmo|cro|cpo|founder|co-founder|cofounder|chief|president|vp|"
                         r"vice president|head of|director|manager|lead|partner|owner|principal|officer|"
                         r"engineer|designer|marketing|sales|growth|revenue|operations|product|recruit|talent)\b", re.I)
NAME_RE = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?: [A-ZÀ-Ý]\.?)?(?: (?:[a-z]{2,3} )?[A-ZÀ-Ý][a-zà-ÿ'’\-]+){1,2}$")
EMAIL_FIND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
NOT_A_NAME = re.compile(
    r"\b(senior|junior|account|executive|representative|manager|director|engineer|sales|marketing|international|"
    r"global|team|our|the|your|about|contact|customer|success|support|learn|read|view|join|meet|get|book|open|"
    r"employment|solutions|services|platform|product|company|careers|privacy|policy|terms|remote|hybrid|new|"
    r"united|states|kingdom|york|san|francisco|london|berlin|press|media|partner|partners|group|inc|labs|"
    r"renewal|strategic|enterprise|commercial|simplify|officer|chief|head|lead|vice|president|business|"
    r"development|operations|specialist|associate|analyst|consultant|intern|design|data|cloud|security)\b", re.I)
TESTIMONIAL = re.compile(r"\b(?:at|@)\s+[A-Z]|,\s+[A-Z][\w&.\- ]+$")
TEAM_PAGE = re.compile(r"team|people|leadership|about|founders|management|who-we-are|company", re.I)
SENIORITY = ["founder", "ceo", "chief", "president", "owner", "partner", "vp", "vice president", "head", "director",
             "lead", "manager"]
GENERIC_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "github.com", "medium.com", "youtube.com",
                 "notion.site", "substack.com", "google.com", "apple.com", "crunchbase.com", "angel.co",
                 "wellfound.com", "linktr.ee", "bit.ly", "t.co", "instagram.com")


def norm_company(name: str) -> str:
    n = re.sub(r"[,.]", "", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|gmbh|corp|corporation|co|company|ag|sa|bv|plc|pty|limited|technologies|labs|hq)\b",
               "", n)
    return re.sub(r"[^a-z0-9]", "", n)


_lookup_sem = asyncio.Semaphore(2)


def domain_from_text(company: str, text: str) -> str:
    """A link in the signal itself whose host is the company's own name, e.g. 'harvey.ai' in Harvey's job ad."""
    key = norm_company(company)
    if len(key) < 3:
        return ""
    for host in re.findall(r"(?:https?://|www\.|@)((?:[a-z0-9\-]+\.)+[a-z]{2,})", (text or "").lower()):
        host = host[4:] if host.startswith("www.") else host
        labels = host.split(".")
        if re.sub(r"[^a-z0-9]", "", labels[-2] if len(labels) > 1 else "") == key and \
                not any(host.endswith(g) for g in GENERIC_HOSTS):
            return ".".join(labels[-2:]) if len(labels[-1]) > 2 or len(labels) == 2 else host
    return ""


async def resolve_domain(company: str, hint_text: str = "") -> str:
    """Company name to website domain. Prefers precision: '' when there is no confident match."""
    key = norm_company(company)
    if not key:
        return ""
    cached = db.cache_get(f"domain:{key}", 30 * 86400)
    if cached is not None:
        return cached
    domain = domain_from_text(company, hint_text)
    if not domain:
        hits = None
        for attempt in range(2):
            try:
                async with _lookup_sem:
                    hits = await web.get_json("https://autocomplete.clearbit.com/v1/companies/suggest",
                                              params={"query": company}, timeout=10)
                break
            except Exception:
                await asyncio.sleep(1.5)
        if hits is None:
            return ""  # lookup is down; do not cache the miss
        for h in hits[:5]:
            hk, d = norm_company(h.get("name", "")), (h.get("domain") or "").lower()
            if hk == key or d.split(".")[0] == key:
                domain = d
                break
    if not domain and len(key) >= 6:
        # Last resort for distinctive names: <name>.com, accepted only if the page names the company.
        for tld in ("com", "io", "ai", "co"):
            r = await web.fetch_page(f"https://{key}.{tld}", timeout=8)
            if r is not None and r.status_code == 200:
                s = web.soup_of(r.text[:60000])
                site = s.find("meta", attrs={"property": "og:site_name"})
                label = ((s.title.get_text() if s.title else "") + " " + (site.get("content", "") if site else ""))
                if key in norm_company(label):
                    domain = web.host_of(str(r.url))
                    break
    db.cache_set(f"domain:{key}", domain)
    return domain


def looks_like_person(name: str, title: str = "") -> bool:
    return bool(NAME_RE.match(name or "")) and not NOT_A_NAME.search(name) and len(name) <= 40


def detect_tech(html: str) -> list[str]:
    return sorted(k for k, rx in TECH_RE.items() if rx.search(html))


def _jsonld_people(soup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("@type") == "Person" and node.get("name"):
                    out.append({"name": str(node["name"]).strip(), "title": str(node.get("jobTitle") or "").strip(),
                                "linkedin_url": next((s for s in ([node.get("sameAs")] if isinstance(node.get("sameAs"), str)
                                                                  else node.get("sameAs") or []) if "linkedin.com/in/" in s), "")})
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def _card_people(soup) -> list[dict]:
    """Team-page cards: a name-shaped heading with a title-shaped line next to it, or a LinkedIn profile link."""
    out = []
    for a in soup.find_all("a", href=re.compile(r"linkedin\.com/in/", re.I)):
        card = a
        for _ in range(4):
            card = card.parent
            if card is None:
                break
            texts = [t.strip() for t in card.stripped_strings if t.strip()]
            name = next((t for t in texts if looks_like_person(t)), "")
            if name:
                title = next((t for t in texts if t != name and TITLE_WORDS.search(t) and len(t) < 80), "")
                out.append({"name": name, "title": title, "linkedin_url": a["href"].split("?")[0]})
                break
    for h in soup.find_all(["h2", "h3", "h4", "h5", "strong", "p", "div", "span"]):
        if h.find(["h2", "h3", "h4", "h5", "div", "p"]):
            continue
        name = h.get_text(" ", strip=True)
        if not looks_like_person(name):
            continue
        sib = h.find_next_sibling() or (h.parent.find_next_sibling() if h.parent else None)
        title = sib.get_text(" ", strip=True) if sib else ""
        if title and len(title) < 80 and TITLE_WORDS.search(title) and not NAME_RE.match(title):
            out.append({"name": name, "title": title, "linkedin_url": ""})
    return out


PEOPLE_SCHEMA = llm.obj({"people": llm.arr(llm.obj({"name": llm.STR, "title": llm.STR}))})


async def _llm_people(text: str) -> list[dict]:
    data = await llm.ask_json(
        "Extract the named employees of this company from its website text. Return only real people with a "
        "stated job title; skip customers, investors, advisors, testimonial authors and blog bylines. "
        "At most 15, most senior first.", text[:9000], PEOPLE_SCHEMA, effort="low", max_tokens=2500)
    return (data or {}).get("people", [])


async def crawl_company(domain: str, use_llm: bool = True) -> dict:
    """Read a company's public site. Returns description, tech, people, emails, phones, linkedin URL."""
    out = {"domain": domain, "ok": False, "title": "", "description": "", "tech": [], "people": [], "emails": [],
           "phones": [], "linkedin_company_url": "", "pages": []}
    home = await web.fetch_page(f"https://{domain}")
    if home is None or home.status_code >= 400:
        home = await web.fetch_page(f"https://www.{domain}")
    if home is None or home.status_code >= 400:
        return out
    base = str(home.url)
    host = web.host_of(base)
    soup = web.soup_of(home.text)
    out["ok"] = True
    out["title"] = (soup.title.get_text(strip=True) if soup.title else "")[:160]
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    region = validate.region_for_domain(domain)

    pages = [(base, home.text)]
    picks, seen = [], {base.rstrip("/")}
    for href, _ in web.links(soup, base):
        clean = href.split("#")[0].split("?")[0].rstrip("/")
        if web.host_of(clean) == host and clean not in seen and PAGE_HINT.search(urlparse(clean).path + "/"):
            seen.add(clean)
            picks.append(clean)
    picks.sort(key=lambda u: (len(urlparse(u).path.split("/")), len(u)))
    fetched = await asyncio.gather(*[web.fetch_page(p) for p in picks[:5]])
    pages += [(str(r.url), r.text) for r in fetched if r is not None and r.status_code == 200]
    out["pages"] = [u for u, _ in pages]

    tech, emails, phones, people, team_text = set(), [], [], [], []
    for url, html in pages:
        tech.update(detect_tech(html))
        s = web.soup_of(html)
        for a in s.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                emails.append(href[7:].split("?")[0])
            elif href.lower().startswith("tel:"):
                phones.append(href[4:])
            elif "linkedin.com/company/" in href and not out["linkedin_company_url"]:
                m = re.search(r"https?://[a-z.]*linkedin\.com/company/[^/?#]+", href, re.I)
                out["linkedin_company_url"] = m.group(0) if m else href.split("?")[0]
        people += _jsonld_people(s)
        if url != base and TEAM_PAGE.search(urlparse(url).path):
            people += _card_people(s)  # only on team-style pages; homepages are full of customer quotes
        text = web.visible_text(s)
        emails += EMAIL_FIND.findall(text)
        if re.search(r"contact|impressum|imprint|about", url, re.I) or url == base:
            phones += validate.find_phones(text, region)
        if re.search(r"team|people|leadership|about|founders|management|company", url, re.I):
            team_text.append(f"# {url}\n{text[:5000]}")
    if not people and team_text and use_llm and llm.available():
        people = await _llm_people("\n\n".join(team_text))

    out["description"] = ((meta.get("content") if meta else "") or web.visible_text(soup, 300)).strip()[:500]
    out["tech"] = sorted(tech)
    seen_e, seen_p, seen_n = set(), set(), set()
    for e in emails:
        e = validate.normalize_email(e)
        if e and e not in seen_e and validate.EMAIL_RE.match(e) and not e.endswith(validate.NOT_EMAIL_SUFFIX):
            seen_e.add(e)
            out["emails"].append(e)
    for p in phones:
        v = validate.validate_phone(p, region)
        if v["valid"] and v["e164"] not in seen_p:
            seen_p.add(v["e164"])
            out["phones"].append(v)
    for p in people:
        key = p.get("name", "").lower()
        if key and key not in seen_n and looks_like_person(p["name"]) and not TESTIMONIAL.search(p.get("title", "")):
            seen_n.add(key)
            out["people"].append({"name": p["name"], "title": p.get("title", ""),
                                  "linkedin_url": p.get("linkedin_url", "")})
    out["people"] = out["people"][:20]
    return out


def rank_people(people: list[dict], icp_titles: list[str]) -> list[dict]:
    """Best buyer match first: ICP title overlap, then seniority."""
    icp_tokens = [set(re.findall(r"[a-z]+", t.lower())) - {"of", "the", "and"} for t in icp_titles]

    def score(p: dict) -> float:
        title = p.get("title", "").lower()
        toks = set(re.findall(r"[a-z]+", title))
        best = max((len(toks & it) / max(1, len(it)) for it in icp_tokens), default=0)
        senior = next((len(SENIORITY) - i for i, s in enumerate(SENIORITY) if s in title), 0)
        return best * 100 + senior
    return sorted(people, key=score, reverse=True)


async def hunter_domain(domain: str) -> list[dict]:
    """Named contacts with real addresses from Hunter (paid, optional)."""
    if not config.HUNTER_API_KEY:
        return []
    try:
        data = await web.get_json("https://api.hunter.io/v2/domain-search",
                                  params={"domain": domain, "limit": 10, "api_key": config.HUNTER_API_KEY}, timeout=25)
    except Exception:
        return []
    out = []
    for e in data.get("data", {}).get("emails", []):
        name = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x)
        out.append({"name": name, "title": e.get("position") or "", "email": e.get("value"),
                    "linkedin_url": e.get("linkedin") or "", "phone": e.get("phone_number") or "",
                    "confidence": e.get("confidence"), "source": "hunter"})
    return out


def linkedin_search_url(company: str, title: str = "", name: str = "") -> str:
    from urllib.parse import quote
    kw = " ".join(x for x in (name, title, company) if x)
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(kw)


async def enrich_lead(lead_id: int) -> dict:
    """Fill in a lead's company facts and contacts. Safe to call again; contacts are only added once."""
    lead = db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not lead:
        return {}
    b = brain_mod.get()
    domain = lead["company_domain"] or ""
    person_only = lead["lead_key"].startswith("p:")
    if person_only and not domain:
        # A person seen in a public thread with no company attached: nothing to look up, work it from the signal.
        db.x("UPDATE leads SET enriched_at=? WHERE id=?", (db.now(), lead_id))
        return {"domain": "", "site_ok": False, "contacts_added": 0, "tech": []}
    if not domain and lead["company_name"]:
        hint = " ".join(f"{s['text'] or ''} {s['extra'].get('external_url', '')}" for s in
                        db.q("SELECT text, extra FROM signals WHERE lead_id=?", (lead_id,)))
        domain = await resolve_domain(lead["company_name"], hint)
    if domain and any(domain.endswith(g) for g in GENERIC_HOSTS):
        domain = ""
    site = {"ok": False, "people": [], "emails": [], "phones": [], "tech": [], "description": "",
            "linkedin_company_url": ""}
    if domain:
        site = await crawl_company(domain)
    db.x("UPDATE leads SET company_domain=?, description=COALESCE(NULLIF(?,''), description), tech=?, "
         "linkedin_company_url=COALESCE(NULLIF(?,''), linkedin_company_url), enriched_at=?, updated_at=? WHERE id=?",
         (domain, site["description"], db.j(site["tech"]), site["linkedin_company_url"], db.now(), db.now(), lead_id))

    existing = {(c["name"] or "").lower() for c in db.q("SELECT name FROM contacts WHERE lead_id=?", (lead_id,))}
    existing_emails = {c["email"] for c in db.q("SELECT email FROM contacts WHERE lead_id=? AND email IS NOT NULL", (lead_id,))}
    company = lead["company_name"] or domain
    main_phone = next((p for p in site["phones"]), None)
    added = 0

    async def add(name, title, email, source, linkedin="", phone=None, guessed=False):
        nonlocal added
        if (name and name.lower() in existing) or (email and email in existing_emails):
            return
        detail = {"status": None}
        if email:
            detail = await validate.validate_email(email)
            if guessed and detail["status"] != "invalid":
                # A pattern guess is only as good as the mail server's answer; never present it as verified.
                detail["flags"].append("pattern_guess")
                if detail["status"] != "valid":
                    detail.update(status="risky", score=min(detail["score"], 45),
                                  reason="guessed from the common first.last pattern; " + detail["reason"])
        if phone and phone.get("e164"):
            tw = await validate.twilio_line_type(phone["e164"])
            if tw:
                phone = {**phone, "twilio": tw}
        db.x("INSERT INTO contacts(lead_id,name,title,email,email_status,email_detail,phone,phone_detail,"
             "linkedin_url,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (lead_id, name, title, email or None, detail.get("status"), db.j(detail),
              (phone or {}).get("e164"), db.j(phone or {}),
              linkedin or linkedin_search_url(company, title, name), source, db.now()))
        existing.add((name or "").lower())
        if email:
            existing_emails.add(email)
        added += 1

    if domain:
        for h in await hunter_domain(domain):
            ph = validate.validate_phone(h["phone"], validate.region_for_domain(domain)) if h["phone"] else None
            await add(h["name"], h["title"], h["email"], "hunter", h["linkedin_url"], ph if ph and ph["valid"] else None)
    for p in rank_people(site["people"], b["icp"]["titles"])[:6]:
        cands = validate.email_candidates(p["name"], domain) if domain else []
        await add(p["name"], p["title"], cands[0] if cands else "", "website", p.get("linkedin_url", ""),
                  main_phone, guessed=True)
    for e in site["emails"][:3]:
        if e.split("@")[-1] == domain or not domain:
            await add("", "Company inbox", e, "website", site["linkedin_company_url"], main_phone)
    if not added and not existing and main_phone:
        await add("", "Main line", "", "website", site["linkedin_company_url"], main_phone)
    if not added and not existing and company:
        # Nothing public on the site: leave a LinkedIn search for the likely buyer so the lead is still workable.
        title = (b["icp"]["titles"] or [""])[0]
        await add("", title or "Decision maker", "", "linkedin_search", linkedin_search_url(company, title))

    set_primary(lead_id)
    return {"domain": domain, "site_ok": site["ok"], "contacts_added": added, "tech": site["tech"]}


def set_primary(lead_id: int) -> None:
    """Primary contact: a named person with a usable email beats a named person, beats a shared inbox."""
    cs = db.q("SELECT * FROM contacts WHERE lead_id=?", (lead_id,))
    if not cs:
        return
    b = brain_mod.get()
    ranked = rank_people([{"id": c["id"], "title": c["title"] or "", "name": c["name"]} for c in cs],
                         b["icp"]["titles"])
    order = {r["id"]: i for i, r in enumerate(ranked)}
    status_rank = {"valid": 0, "risky": 1, "unknown": 2, None: 3, "invalid": 4}

    def key(c):
        return (0 if c["name"] else 1, status_rank.get(c["email_status"], 3), order.get(c["id"], 99))
    best = sorted(cs, key=key)[0]
    db.x("UPDATE contacts SET is_primary = CASE WHEN id=? THEN 1 ELSE 0 END WHERE lead_id=?", (best["id"], lead_id))
