"""Runtime settings, all from environment variables. Secrets never go in the database."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = env("SIGNALPIPE_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "signalpipe.db")

# Access control. With no password set the app is open, which is only right on localhost.
APP_PASSWORD = env("APP_PASSWORD")
API_TOKEN = env("API_TOKEN")  # bearer token for the MCP server and scripts
SECRET_KEY = env("SECRET_KEY") or APP_PASSWORD or "dev-only-secret"

# Claude. The Anthropic SDK reads ANTHROPIC_API_KEY itself.
LLM_MODEL = env("SIGNALPIPE_MODEL", "claude-opus-5-5")
LLM_ENABLED = env("SIGNALPIPE_LLM", "1") != "0"

USER_AGENT = env("SIGNALPIPE_USER_AGENT", "signalpipe/1.0 (+lead research; respects robots.txt)")

# Outbound email, through your own mailbox.
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM") or SMTP_USER
SMTP_FROM_NAME = env("SMTP_FROM_NAME")
SMTP_SECURITY = env("SMTP_SECURITY", "starttls")  # starttls | ssl | none
EMAIL_DAILY_CAP = _int("EMAIL_DAILY_CAP", 30)
POSTAL_ADDRESS = env("POSTAL_ADDRESS")  # CAN-SPAM requires a physical address in the footer

# Reply detection.
IMAP_HOST = env("IMAP_HOST")
IMAP_PORT = _int("IMAP_PORT", 993)
IMAP_USER = env("IMAP_USER") or SMTP_USER
IMAP_PASS = env("IMAP_PASS") or SMTP_PASS

# Optional paid data providers; each one is skipped when its key is absent.
HUNTER_API_KEY = env("HUNTER_API_KEY")
GITHUB_TOKEN = env("GITHUB_TOKEN")
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
SMTP_PROBE = env("SMTP_PROBE", "0") == "1"  # RCPT probing needs outbound port 25

# Pacing.
RUN_INTERVAL_HOURS = _int("RUN_INTERVAL_HOURS", 12)
ENRICH_PER_RUN = _int("ENRICH_PER_RUN", 40)
LINKEDIN_DAILY_CAP = _int("LINKEDIN_DAILY_CAP", 20)
DEFAULT_REGION = env("DEFAULT_PHONE_REGION", "US")


def status() -> dict:
    """What is configured, without leaking any secret values."""
    return {
        "llm": bool(LLM_ENABLED and (env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"))),
        "llm_model": LLM_MODEL,
        "smtp": bool(SMTP_HOST and SMTP_FROM),
        "imap": bool(IMAP_HOST and IMAP_USER),
        "hunter": bool(HUNTER_API_KEY),
        "github_token": bool(GITHUB_TOKEN),
        "twilio_lookup": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "smtp_probe": SMTP_PROBE,
        "auth": bool(APP_PASSWORD),
        "email_daily_cap": EMAIL_DAILY_CAP,
        "linkedin_daily_cap": LINKEDIN_DAILY_CAP,
        "run_interval_hours": RUN_INTERVAL_HOURS,
        "postal_address": bool(POSTAL_ADDRESS),
    }
