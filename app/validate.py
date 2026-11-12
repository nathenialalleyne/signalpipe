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


def _smtp_probe(mx: str, address: str, helo_domain: str) -> tuple[str, str]:
    """RCPT TO probe without sending mail. Returns (accept|reject|unknown, detail)."""
    try:
        with smtplib.SMTP(mx, 25, timeout=8, local_hostname=helo_domain) as s:
            s.ehlo()
            s.mail(f"verify@{helo_domain}")
            code, msg = s.rcpt(address)
            if code in (250, 251):
                return "accept", str(code)
            if code in (550, 551, 553):
                return "reject", f"{code} {msg[:80]!r}"
            return "unknown", f"{code}"
    except (socket.timeout, OSError, smtplib.SMTPException) as e:
        return "unknown", type(e).__name__


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
        helo = web.host_of(config.SMTP_FROM.split("@")[-1]) if config.SMTP_FROM else "localhost.localdomain"
        res, detail = await asyncio.to_thread(_smtp_probe, mx[0], email, helo)
        if res == "reject":
            status, score, reason = "invalid", 0, f"mailbox rejected ({detail})"
        elif res == "accept":
            bogus = f"zz-no-such-user-{abs(hash(domain)) % 99999}@{domain}"
            res2, _ = await asyncio.to_thread(_smtp_probe, mx[0], bogus, helo)
            if res2 == "accept":
                flags.append("catch_all")
                status, score, reason = "risky", 60, "catch-all domain accepts every address"
            else:
                status, score, reason = "valid", 95, "mailbox accepted by mail server"
        else:
            flags.append("probe_unavailable")
    if status != "invalid":
        if "role" in flags:
            score -= 15
            reason += "; shared role inbox"
        if "free_mail" in flags:
            score -= 5
    out.update(status=status, score=max(0, score), reason=reason, flags=flags)
    return out


def email_candidates(name: str, domain: str) -> list[str]:
    """Common corporate address patterns, most frequent first."""
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[\s\-]+", (name or "").lower())]
    parts = [p for p in parts if p]
    if not parts or not domain:
        return []
    f, last = parts[0], parts[-1] if len(parts) > 1 else ""
    pats = [f"{f}@{domain}"]
    if last:
        pats = [f"{f}.{last}@{domain}", f"{f}@{domain}", f"{f[0]}{last}@{domain}", f"{f}{last}@{domain}",
                f"{f}{last[0]}@{domain}", f"{f}_{last}@{domain}", f"{last}@{domain}"]
    return pats


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
