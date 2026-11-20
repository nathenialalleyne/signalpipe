"""Work out mail server settings from an email address, and test them without sending anything.

Presets cover the common providers. For a company domain, the provider is read off the domain's MX
records (Google Workspace, Microsoft 365, Zoho, Fastmail). Anything else needs the hosts typed in.
"""
import asyncio
import imaplib
import smtplib
import ssl

from . import config, validate

PRESETS = {
    "google": {
        "label": "Google (Gmail or Workspace)",
        "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": 587, "SMTP_SECURITY": "starttls",
        "IMAP_HOST": "imap.gmail.com", "IMAP_PORT": 993,
        "help": "Use an app password, not your normal one: in your Google Account, turn on 2-Step "
                "Verification, then create an app password and paste it here.",
    },
    "microsoft": {
        "label": "Microsoft 365 or Outlook",
        "SMTP_HOST": "smtp.office365.com", "SMTP_PORT": 587, "SMTP_SECURITY": "starttls",
        "IMAP_HOST": "outlook.office365.com", "IMAP_PORT": 993,
        "help": "Microsoft often switches off password sign-in for mail apps. If the test fails, your "
                "administrator has to allow it for this mailbox, or use another mailbox.",
    },
    "icloud": {
        "label": "iCloud Mail",
        "SMTP_HOST": "smtp.mail.me.com", "SMTP_PORT": 587, "SMTP_SECURITY": "starttls",
        "IMAP_HOST": "imap.mail.me.com", "IMAP_PORT": 993,
        "help": "Use an app-specific password, created in your Apple Account's sign-in and security settings.",
    },
    "zoho": {
        "label": "Zoho Mail",
        "SMTP_HOST": "smtp.zoho.com", "SMTP_PORT": 587, "SMTP_SECURITY": "starttls",
        "IMAP_HOST": "imap.zoho.com", "IMAP_PORT": 993,
        "help": "If two-factor sign-in is on, use an app password. Accounts in the EU or India use "
                "zoho.eu or zoho.in in these host names.",
    },
    "fastmail": {
        "label": "Fastmail",
        "SMTP_HOST": "smtp.fastmail.com", "SMTP_PORT": 465, "SMTP_SECURITY": "ssl",
        "IMAP_HOST": "imap.fastmail.com", "IMAP_PORT": 993,
        "help": "Use an app password from Fastmail's settings.",
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "SMTP_HOST": "smtp.mail.yahoo.com", "SMTP_PORT": 465, "SMTP_SECURITY": "ssl",
        "IMAP_HOST": "imap.mail.yahoo.com", "IMAP_PORT": 993,
        "help": "Use an app password from Yahoo's account security settings.",
    },
}
BY_DOMAIN = {
    "gmail.com": "google", "googlemail.com": "google",
    "outlook.com": "microsoft", "hotmail.com": "microsoft", "live.com": "microsoft", "msn.com": "microsoft",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
    "zoho.com": "zoho", "zohomail.com": "zoho",
    "fastmail.com": "fastmail", "fastmail.fm": "fastmail",
    "yahoo.com": "yahoo", "ymail.com": "yahoo",
}
BY_MX = (("google.com", "google"), ("googlemail.com", "google"), ("outlook.com", "microsoft"),
         ("zoho.com", "zoho"), ("zoho.eu", "zoho"), ("messagingengine.com", "fastmail"))


async def guess(email: str) -> dict:
    email = validate.normalize_email(email)
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    name = BY_DOMAIN.get(domain)
    if not name and domain:
        mx = await validate.mx_hosts(domain) or []
        name = next((p for suffix, p in BY_MX for h in mx if h.lower().endswith(suffix)), None)
    if not name:
        return {"provider": None, "label": "", "help": "We could not recognise this provider. Enter its outgoing "
                "(SMTP) and incoming (IMAP) server names, which your provider's help pages list."}
    p = PRESETS[name]
    return {"provider": name, "label": p["label"], "help": p["help"],
            "settings": {k: v for k, v in p.items() if k.isupper()}}


def _test_smtp() -> str:
    ctx = ssl.create_default_context()
    server = (smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20, context=ctx)
              if config.SMTP_SECURITY == "ssl" else smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20))
    with server:
        server.ehlo()
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASS)
    return "ok"


def _test_imap() -> str:
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=20) as im:
        im.login(config.IMAP_USER, config.IMAP_PASS)
        im.select("INBOX", readonly=True)
    return "ok"


def _plain(e: Exception) -> str:
    text = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "AUTHENTICATIONFAILED" in text.upper() or "535" in text:
        return "The server rejected the address or password. Most providers need an app password here."
    if isinstance(e, (TimeoutError, ConnectionRefusedError, OSError)) and not isinstance(e, ssl.SSLError):
        return f"Could not reach the server ({type(e).__name__}). Check the host name and port."
    return f"{type(e).__name__}: {text[:160]}"


async def test() -> dict:
    """Sign in to both servers without sending or changing anything."""
    out = {}
    for key, ready, fn in (("sending", config.SMTP_HOST, _test_smtp), ("replies", config.IMAP_HOST, _test_imap)):
        if not ready:
            out[key] = {"ok": False, "message": "Not set up yet."}
            continue
        try:
            await asyncio.to_thread(fn)
            out[key] = {"ok": True, "message": "Signed in."}
        except Exception as e:  # report every failure in plain words; nothing here should crash the page
            out[key] = {"ok": False, "message": _plain(e)}
    return out
