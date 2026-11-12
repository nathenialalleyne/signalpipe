"""Email and phone validation.

Email: syntax, disposable/role/free-mail flags, MX lookup (system DNS, then DNS-over-HTTPS),
optional SMTP RCPT probe with catch-all detection, optional Hunter verifier.
Phone: libphonenumber validity, line type, region, carrier, timezone and a local calling window;
optional Twilio Lookup line-type check.
"""
import asyncio
import datetime as dt
import re
import smtplib
import socket
import zoneinfo

import phonenumbers
from phonenumbers import carrier as pn_carrier
from phonenumbers import geocoder as pn_geocoder
from phonenumbers import timezone as pn_timezone

from . import config, db, web

EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+$")
ROLE_LOCALS = {
    "info", "hello", "contact", "sales", "support", "admin", "team", "office", "help", "careers", "jobs",
    "press", "media", "marketing", "billing", "legal", "privacy", "security", "abuse", "noreply", "no-reply",
    "hr", "enquiries", "inquiries", "mail", "webmaster", "service", "hi", "partners", "partnerships", "founders",
}
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "mail.com", "yandex.com", "zoho.com", "hey.com",
    "fastmail.com", "qq.com", "163.com", "msn.com",
}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "fakeinbox.com", "mailnesia.com", "moakt.com", "emailondeck.com", "tempail.com", "mintemail.com",
}
PLACEHOLDER_LOCALS = {"example", "test", "user", "name", "you", "your", "email", "john.doe", "jane.doe", "firstname"}
NOT_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


def normalize_email(raw: str) -> str:
    return (raw or "").strip().strip("<>.,;:'\"").lower()


async def mx_hosts(domain: str) -> list[str] | None:
    """MX hosts for a domain, best first. [] means the domain cannot receive mail; None means lookup failed."""
    cached = db.cache_get(f"mx:{domain}", 7 * 86400)
    if cached is not None:
        return cached
    hosts: list[str] | None = None
    try:
        import dns.resolver

        def _resolve():
            ans = dns.resolver.resolve(domain, "MX", lifetime=4)
            return [str(r.exchange).rstrip(".") for r in sorted(ans, key=lambda r: r.preference)]

        hosts = await asyncio.to_thread(_resolve)
    except Exception as e:  # NXDOMAIN is authoritative; anything else falls through to DoH
        if type(e).__name__ == "NXDOMAIN":
            hosts = []
    if hosts is None:
        for url in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
            try:
                data = await web.get_json(url, params={"name": domain, "type": "MX"},
                                          headers={"accept": "application/dns-json"}, timeout=8)
                if data.get("Status") == 3:
                    hosts = []
                elif data.get("Status") == 0:
                    recs = []
                    for a in data.get("Answer", []) or []:
                        if a.get("type") == 15:
                            pref, _, host = a["data"].partition(" ")
                            recs.append((int(pref), host.rstrip(".")))
                    hosts = [h for _, h in sorted(recs) if h]
                if hosts is not None:
                    break
            except Exception:
                continue
    if hosts is not None:
        db.cache_set(f"mx:{domain}", hosts)
    return hosts


MAIL_PROVIDERS = (
    ("google", ("google.com", "googlemail.com")), ("microsoft", ("outlook.com",)),
    ("proofpoint", ("pphosted.com",)), ("mimecast", ("mimecast.com",)), ("zoho", ("zoho.com", "zoho.eu")),
    ("barracuda", ("barracudanetworks.com",)), ("fastmail", ("messagingengine.com",)),
)
USER_UNKNOWN = re.compile(r"user unknown|no such user|does not exist|unknown recipient|"
                          r"recipient (address )?rejected|invalid recipient|mailbox (not found|unavailable)|no mailbox", re.I)
SENDER_BLOCKED = re.compile(r"5\.7\.\d|blocked|blacklist|blocklist|spamhaus|denied|not allowed|policy|reputation|"
                            r"reverse dns|ptr record|banned|refused", re.I)
MAILBOX_GONE_CODE = re.compile(r"\b5\.(1\.1|1\.10|4\.1)\b")  # enhanced status codes that mean "no such mailbox"
PROBE_PORT, PROBE_PAUSE, GREYLIST_WAIT = 25, 0.6, 4.0
_probe_locks: dict[str, asyncio.Lock] = {}


def mail_provider(mx: list[str]) -> str:
    """Who hosts the domain's mail, read off its MX records."""
    host = (mx or [""])[0].lower()
    return next((name for name, ends in MAIL_PROVIDERS if host.endswith(ends)), "other")


def looks_gibberish(local: str) -> bool:
    """Random-looking local parts (generated or throwaway addresses)."""
    if len(local) < 8:
        return False
    letters = re.sub(r"[^a-z]", "", local)
    if sum(ch.isdigit() for ch in local) / len(local) > 0.5:
        return True
    if len(letters) >= 8 and not re.search(r"[aeiouy]", letters):
        return True
    return bool(re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", letters))


def _helo() -> str:
    """Name we introduce ourselves with. Mail servers trust it more when it matches the IP's reverse DNS."""
    if config.env("SMTP_HELO"):
        return config.env("SMTP_HELO")
    return web.host_of(config.SMTP_FROM.split("@")[-1]) if config.SMTP_FROM else "localhost.localdomain"


def _smtp_probe(mx: str, address: str, helo_domain: str) -> tuple[str, str]:
    """Ask the mail server whether it would accept mail for `address`, then hang up without sending.

    Returns (accept | reject | blocked | greylisted | unknown, detail). `blocked` means the server refused
    *us*, which says nothing about the mailbox.
    """
    mail_from = config.SMTP_FROM or f"verify@{helo_domain}"
    try:
        with smtplib.SMTP(mx, PROBE_PORT, timeout=10, local_hostname=helo_domain) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                try:
                    s.starttls()
                    s.ehlo()
                except (smtplib.SMTPException, OSError):
                    pass
            code, msg = s.mail(mail_from)
            if code >= 400:
                return "blocked", f"{code} at MAIL FROM"
            code, raw = s.rcpt(address)
            msg = raw.decode("utf-8", "replace")[:160] if isinstance(raw, bytes) else str(raw)[:160]
            if code in (250, 251):
                return "accept", str(code)
            if 400 <= code < 500:
                return "greylisted", f"{code} {msg}"
            if code >= 500:
                if MAILBOX_GONE_CODE.search(msg):
                    return "reject", f"{code} {msg}"
                if SENDER_BLOCKED.search(msg):
                    return "blocked", f"{code} {msg}"
                if USER_UNKNOWN.search(msg):
                    return "reject", f"{code} {msg}"
                # A bare 5xx with wording we do not recognise is not proof the mailbox is missing.
                return "unknown", f"{code} {msg}"
            return "unknown", str(code)
    except (socket.timeout, OSError, smtplib.SMTPException) as e:
        return "unknown", type(e).__name__


async def _probe(mx: str, address: str) -> tuple[str, str]:
    domain = address.rsplit("@", 1)[-1]
    lock = _probe_locks.setdefault(domain, asyncio.Lock())
    async with lock:  # one conversation per domain at a time, with a pause between them
        res = await asyncio.to_thread(_smtp_probe, mx, address, _helo())
        await asyncio.sleep(PROBE_PAUSE)
    return res


async def domain_is_catch_all(domain: str, mx: list[str]) -> bool | None:
    """True when the server accepts mail for an address that cannot exist. None when it would not tell us."""
    cached = db.cache_get(f"catchall:{domain}", 14 * 86400)
    if cached is not None:
        return cached
    res, _ = await _probe(mx[0], f"zz-no-such-mailbox-{abs(hash(domain)) % 99991}@{domain}")
    answer = True if res == "accept" else False if res == "reject" else None
    if answer is not None:
        db.cache_set(f"catchall:{domain}", answer)
    return answer


async def smtp_verify(email: str, mx: list[str]) -> tuple[str, str]:
    """Mailbox-level check. Returns (valid | invalid | catch_all | unknown, detail)."""
    domain = email.rsplit("@", 1)[-1]
    catch_all = await domain_is_catch_all(domain, mx)
    if catch_all is True:
        return "catch_all", "domain accepts every address"
    if catch_all is None:
        return "unknown", "mail server would not answer a verification probe"
    res, detail = await _probe(mx[0], email)
    if res == "greylisted":  # temporary deferral: one polite retry
        await asyncio.sleep(GREYLIST_WAIT)
        res, detail = await _probe(mx[0], email)
    return {"accept": "valid", "reject": "invalid"}.get(res, "unknown"), detail


async def validate_email(raw: str, deep: bool = True) -> dict:
    """Returns {email, status: valid|risky|invalid|unknown, score: 0-100, flags: [...], reason}."""
    email = normalize_email(raw)
    out = {"email": email, "status": "invalid", "score": 0, "flags": [], "reason": ""}
    if not email or not EMAIL_RE.match(email) or email.endswith(NOT_EMAIL_SUFFIX) or ".." in email:
        out["reason"] = "bad syntax"
        return out
    local, domain = email.rsplit("@", 1)
    if db.one("SELECT 1 FROM suppression WHERE value IN (?,?)", (email, domain)):
        out.update(status="invalid", reason="on suppression list", flags=["suppressed"])
        return out
    if domain in DISPOSABLE_DOMAINS:
        out.update(reason="disposable domain", flags=["disposable"])
        return out
    if local in PLACEHOLDER_LOCALS or domain in ("example.com", "domain.com", "email.com", "sentry.io", "wixpress.com"):
        out["reason"] = "placeholder address"
        return out
    flags = []
    if local in ROLE_LOCALS or local.split("+")[0] in ROLE_LOCALS:
        flags.append("role")
    if domain in FREE_DOMAINS:
        flags.append("free_mail")

    mx = await mx_hosts(domain)
    if mx == []:
        out.update(reason="domain has no mail servers", flags=flags + ["no_mx"])
        return out
    if mx is None:
        out.update(status="unknown", score=30, reason="DNS lookup failed", flags=flags)
        return out

    score, status, reason = 70, "risky", "domain accepts mail; mailbox not verified"
    if deep and config.HUNTER_API_KEY:
        try:
            data = await web.get_json("https://api.hunter.io/v2/email-verifier",
                                      params={"email": email, "api_key": config.HUNTER_API_KEY}, timeout=25)
            d = data.get("data", {})
            hs = d.get("status")
            flags.append(f"hunter:{hs}")
            if hs == "valid":
                status, score, reason = "valid", max(90, int(d.get("score") or 90)), "verified by Hunter"
            elif hs == "invalid":
                status, score, reason = "invalid", 0, "rejected by Hunter"
            elif hs == "accept_all":
                flags.append("catch_all")
                status, score, reason = "risky", 60, "catch-all domain"
        except Exception:
            flags.append("hunter:error")
    elif deep and config.SMTP_PROBE and "free_mail" not in flags:
        res, detail = await smtp_verify(email, mx)
        if res == "invalid":
            status, score, reason = "invalid", 0, f"mailbox rejected by its mail server ({detail[:60]})"
        elif res == "valid":
            flags.append("smtp_verified")
            status, score, reason = "valid", 95, "mailbox accepted by its mail server"
        elif res == "catch_all":
            flags.append("catch_all")
            status, score, reason = "risky", 60, "catch-all domain accepts every address, so the mailbox cannot be confirmed"
        else:
            flags.append("probe_unavailable")
            reason = "domain accepts mail; " + detail
    provider = mail_provider(mx)
    if provider != "other":
        flags.append(f"mx:{provider}")
    if looks_gibberish(local):
        flags.append("gibberish")
        if status != "invalid":
            score -= 25
            reason += "; address looks randomly generated"
    if status != "invalid":
        if "role" in flags:
            score -= 15
            reason += "; shared role inbox"
        if "free_mail" in flags:
            score -= 5
    out.update(status=status, score=max(0, score), reason=reason, flags=flags)
    return out


PATTERNS = ["first.last", "first", "flast", "firstlast", "firstl", "first_last", "last"]


def _name_parts(name: str) -> tuple[str, str]:
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[\s\-]+", (name or "").lower())]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def email_candidates(name: str, domain: str) -> list[str]:
    """Common corporate address patterns in PATTERNS order, most frequent first."""
    f, last = _name_parts(name)
    if not f or not domain:
        return []
    if not last:
        return [f"{f}@{domain}"]
    return [f"{f}.{last}@{domain}", f"{f}@{domain}", f"{f[0]}{last}@{domain}", f"{f}{last}@{domain}",
            f"{f}{last[0]}@{domain}", f"{f}_{last}@{domain}", f"{last}@{domain}"]


def pattern_of(email: str, name: str) -> str | None:
    """Which naming pattern produces this address for this person, if any."""
    email = normalize_email(email)
    cands = email_candidates(name, email.rsplit("@", 1)[-1]) if "@" in email else []
    return PATTERNS[cands.index(email)] if len(cands) == len(PATTERNS) and email in cands else None


def learn_pattern(domain: str, email: str, name: str) -> str | None:
    """Remember a company's address pattern from one real address, so the rest of the team can be derived."""
    p = pattern_of(email, name)
    if p and p != "first":  # a bare first name is too often a one-off for founders
        db.cache_set(f"pattern:{domain}", p)
    return p


def known_pattern(domain: str) -> str | None:
    return db.cache_get(f"pattern:{domain}", 180 * 86400)


def ordered_candidates(name: str, domain: str) -> list[str]:
    cands = email_candidates(name, domain)
    p = known_pattern(domain)
    if p and len(cands) == len(PATTERNS):
        i = PATTERNS.index(p)
        cands = [cands[i]] + cands[:i] + cands[i + 1:]
    return cands


async def find_email(name: str, domain: str) -> dict:
    """Best address for a named person at a domain, with an honest account of how sure we are.

    With mailbox probing available it tries each pattern until the mail server accepts one. Otherwise it
    returns the company's known pattern (learned from a real address) or the most common pattern.
    """
    cands = ordered_candidates(name, domain)
    if not cands:
        return {"email": "", "status": None, "score": 0, "flags": [], "reason": "no name to build an address from"}
    pattern = known_pattern(domain)
    if config.SMTP_PROBE and domain not in FREE_DOMAINS and not config.HUNTER_API_KEY:
        mx = await mx_hosts(domain)
        if mx and await domain_is_catch_all(domain, mx) is False:
            for cand in cands[:5]:
                res, _ = await smtp_verify(cand, mx)
                if res == "valid":
                    learn_pattern(domain, cand, name)
                    out = await validate_email(cand, deep=False)
                    out.update(status="valid", score=95, reason="mailbox accepted by its mail server",
                               flags=out["flags"] + ["smtp_verified"])
                    return out
                if res != "invalid":
                    break  # server stopped answering; do not keep knocking
            else:
                return {"email": "", "status": None, "score": 0, "flags": ["no_pattern_matched"],
                        "reason": "none of the common address patterns is a real mailbox"}
    out = await validate_email(cands[0], deep=True)
    if out["status"] in ("invalid", "valid"):
        return out
    if pattern:
        out["flags"].append("pattern_confirmed")
        out.update(status="risky", score=min(out["score"], 70),
                   reason=f"follows this company's confirmed {pattern} pattern; " + out["reason"])
    else:
        out["flags"].append("pattern_guess")
        out.update(status="risky", score=min(out["score"], 45),
                   reason="guessed from the most common pattern; " + out["reason"])
    return out


# ---------- phones ----------

PHONE_TYPES = {
    phonenumbers.PhoneNumberType.MOBILE: "mobile",
    phonenumbers.PhoneNumberType.FIXED_LINE: "landline",
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "landline_or_mobile",
    phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
    phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium",
    phonenumbers.PhoneNumberType.VOIP: "voip",
    phonenumbers.PhoneNumberType.SHARED_COST: "shared_cost",
    phonenumbers.PhoneNumberType.UAN: "company_switchboard",
    phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "personal",
    phonenumbers.PhoneNumberType.PAGER: "pager",
    phonenumbers.PhoneNumberType.VOICEMAIL: "voicemail",
}
CALL_START, CALL_END = 9, 17  # local business hours


def region_for_domain(domain: str) -> str:
    tld = (domain or "").rsplit(".", 1)[-1].upper()
    special = {"UK": "GB", "COM": "", "IO": "", "CO": "", "AI": "", "NET": "", "ORG": "", "APP": "", "DEV": ""}
    tld = special.get(tld, tld)
    return tld if tld in phonenumbers.SUPPORTED_REGIONS else config.DEFAULT_REGION


def validate_phone(raw: str, region: str = "") -> dict:
    """Returns {valid, e164, display, type, region, location, carrier, timezones, callable, call_window, flags}."""
    out = {"input": raw, "valid": False, "flags": []}
    try:
        num = phonenumbers.parse(raw, region or config.DEFAULT_REGION)
    except phonenumbers.NumberParseException as e:
        out["reason"] = str(e)
        return out
    if not phonenumbers.is_possible_number(num) or not phonenumbers.is_valid_number(num):
        out["reason"] = "not a valid number for its region"
        return out
    ntype = PHONE_TYPES.get(phonenumbers.number_type(num), "unknown")
    zones = [z for z in pn_timezone.time_zones_for_number(num) if z != "Etc/Unknown"]
    out.update(
        valid=True,
        e164=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164),
        display=phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
        type=ntype,
        region=phonenumbers.region_code_for_number(num),
        location=pn_geocoder.description_for_number(num, "en"),
        carrier=pn_carrier.name_for_number(num, "en"),
        timezones=zones,
    )
    flags = []
    if ntype in ("toll_free", "company_switchboard", "shared_cost"):
        flags.append("main_line")  # reaches a switchboard, not a person
    if ntype in ("premium", "pager", "voicemail"):
        flags.append("do_not_dial")
    if ntype in ("mobile", "landline_or_mobile") and out["region"] in ("US", "CA"):
        flags.append("tcpa_manual_dial_only")  # never autodial or send prerecorded audio to US mobiles
    out["flags"] = flags
    out["callable"] = "do_not_dial" not in flags
    out["call_window"] = call_window(zones)
    return out


def call_window(zones: list[str], now: dt.datetime | None = None) -> dict:
    """Whether it is business hours for the number right now, and the next opening in UTC."""
    if not zones:
        return {"open_now": None, "local_time": None, "next_open_utc": None}
    now = now or dt.datetime.now(dt.timezone.utc)
    # With several possible zones (e.g. a US toll-free number), require all of them to be in hours.
    locals_ = []
    for z in zones[:6]:
        try:
            locals_.append(now.astimezone(zoneinfo.ZoneInfo(z)))
        except Exception:
            continue
    if not locals_:
        return {"open_now": None, "local_time": None, "next_open_utc": None}
    open_now = all(l.weekday() < 5 and CALL_START <= l.hour < CALL_END for l in locals_)
    first = locals_[0]
    nxt = first
    if not open_now:
        nxt = first.replace(hour=CALL_START, minute=0, second=0, microsecond=0)
        if first.hour >= CALL_START:
            nxt += dt.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += dt.timedelta(days=1)
    return {
        "open_now": open_now,
        "local_time": first.strftime("%a %H:%M"),
        "timezone": zones[0],
        "next_open_utc": nxt.astimezone(dt.timezone.utc).isoformat(timespec="minutes"),
    }


async def twilio_line_type(e164: str) -> dict | None:
    """Carrier-grade line type from Twilio Lookup v2 (paid per lookup). None when not configured."""
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN):
        return None
    try:
        data = await web.get_json(f"https://lookups.twilio.com/v2/PhoneNumbers/{e164}",
                                  params={"Fields": "line_type_intelligence"},
                                  auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=15)
        lti = data.get("line_type_intelligence") or {}
        return {"valid": data.get("valid"), "type": lti.get("type"), "carrier": lti.get("carrier_name")}
    except Exception:
        return None


EMAIL_FIND_ANY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def find_phones(text: str, region: str) -> list[str]:
    """Phone-looking strings in free text that parse as valid numbers. Returns E.164, deduplicated."""
    seen, out = set(), []
    for m in phonenumbers.PhoneNumberMatcher(text or "", region or config.DEFAULT_REGION,
                                             leniency=phonenumbers.Leniency.VALID):
        e = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out
