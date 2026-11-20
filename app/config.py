"""Runtime settings. Read from environment variables, and from the Connections form in Settings, whose saved
values take precedence. Every module reads these at call time (config.SMTP_HOST), so a change applies at once."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default) or default)
    except ValueError:
        return default


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Settings the person can change from the dashboard, and which of them are secret (never sent back to the page).
EDITABLE = (
    "ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "SMTP_FROM_NAME",
    "SMTP_SECURITY", "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASS", "POSTAL_ADDRESS", "EMAIL_DAILY_CAP",
    "HUNTER_API_KEY", "LINKEDIN_DAILY_CAP", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "LINKEDIN_TZ",
    "SIGNALPIPE_MODEL", "RESEARCH_PER_RUN", "AGENT_BUDGET_USD", "SIGNALPIPE_ENGINE", "CLAUDE_CODE_OAUTH_TOKEN",
)
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
ENGINES = ("api", "claude_code")  # an Anthropic API key, or Claude Code signed in to your Claude subscription
SECRETS = frozenset({"ANTHROPIC_API_KEY", "SMTP_PASS", "IMAP_PASS", "HUNTER_API_KEY", "LINKEDIN_PASSWORD",
                     "CLAUDE_CODE_OAUTH_TOKEN"})


def _load() -> None:
    g = globals()
    g["LOCAL"] = env("SIGNALPIPE_LOCAL") == "1"  # started by the local launcher on the person's own computer
    g["DATA_DIR"] = env("SIGNALPIPE_DATA", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    g["DB_PATH"] = os.path.join(g["DATA_DIR"], "signalpipe.db")

    # Access control. With no password set the app is open, which is only right on localhost.
    g["APP_PASSWORD"] = env("APP_PASSWORD")
    g["API_TOKEN"] = env("API_TOKEN")  # bearer token for the agent, the MCP server and scripts
    g["SECRET_KEY"] = env("SECRET_KEY") or g["APP_PASSWORD"] or "dev-only-secret"

    # Claude. The Anthropic SDK reads ANTHROPIC_API_KEY itself.
    g["LLM_MODEL"] = env("SIGNALPIPE_MODEL", "claude-opus-5-5")
    g["LLM_ENABLED"] = env("SIGNALPIPE_LLM", "1") != "0"
    g["ENGINE"] = env("SIGNALPIPE_ENGINE", "api") if env("SIGNALPIPE_ENGINE", "api") in ENGINES else "api"
    g["USER_AGENT"] = env("SIGNALPIPE_USER_AGENT", "signalpipe/1.0 (+lead research; respects robots.txt)")

    # Outbound email, through your own mailbox.
    g["SMTP_HOST"] = env("SMTP_HOST")
    g["SMTP_PORT"] = _int("SMTP_PORT", 587)
    g["SMTP_USER"] = env("SMTP_USER")
    g["SMTP_PASS"] = env("SMTP_PASS")
    g["SMTP_FROM"] = env("SMTP_FROM") or g["SMTP_USER"]
    g["SMTP_FROM_NAME"] = env("SMTP_FROM_NAME")
    g["SMTP_SECURITY"] = env("SMTP_SECURITY", "starttls") or "starttls"  # starttls | ssl | none
    g["EMAIL_DAILY_CAP"] = _int("EMAIL_DAILY_CAP", 30)
    g["POSTAL_ADDRESS"] = env("POSTAL_ADDRESS")  # CAN-SPAM requires a physical address in the footer

    # Reply detection.
    g["IMAP_HOST"] = env("IMAP_HOST")
    g["IMAP_PORT"] = _int("IMAP_PORT", 993)
    g["IMAP_USER"] = env("IMAP_USER") or g["SMTP_USER"]
    g["IMAP_PASS"] = env("IMAP_PASS") or g["SMTP_PASS"]

    # Optional paid data providers; each one is skipped when its key is absent.
    g["HUNTER_API_KEY"] = env("HUNTER_API_KEY")
    g["GITHUB_TOKEN"] = env("GITHUB_TOKEN")
    g["TWILIO_ACCOUNT_SID"] = env("TWILIO_ACCOUNT_SID")
    g["TWILIO_AUTH_TOKEN"] = env("TWILIO_AUTH_TOKEN")
    g["SMTP_PROBE"] = env("SMTP_PROBE", "0") == "1"  # RCPT probing needs outbound port 25

    # Pacing.
    g["RUN_INTERVAL_HOURS"] = _int("RUN_INTERVAL_HOURS", 12)
    g["ENRICH_PER_RUN"] = _int("ENRICH_PER_RUN", 40)
    g["RESEARCH_PER_RUN"] = _int("RESEARCH_PER_RUN", 5)
    try:  # monthly cap on estimated Claude spend; every AI feature pauses when it is reached (0 means no cap)
        g["AGENT_BUDGET_USD"] = float(env("AGENT_BUDGET_USD", "20") or 20)
    except ValueError:
        g["AGENT_BUDGET_USD"] = 20.0
    g["LINKEDIN_DAILY_CAP"] = _int("LINKEDIN_DAILY_CAP", 20)
    g["DEFAULT_REGION"] = env("DEFAULT_PHONE_REGION", "US")


_load()
_from_env = {k: os.environ.get(k) for k in EDITABLE}  # what the environment said before any saved values


def apply(saved: dict) -> None:
    """Make saved dashboard values current. A blank saved value falls back to the environment's value."""
    for k in EDITABLE:
        value = str(saved.get(k) or "").strip()
        if value:
            os.environ[k] = value
        elif _from_env.get(k) is not None:
            os.environ[k] = _from_env[k]
        else:
            os.environ.pop(k, None)
    _load()


def status() -> dict:
    """What is configured, without leaking any secret values."""
    from . import claude_code
    api_key = bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"))
    return {
        "llm": bool(LLM_ENABLED and (claude_code.installed() if ENGINE == "claude_code" else api_key)),
        "llm_model": LLM_MODEL,
        "engine": ENGINE,
        "api_key": api_key,
        "claude_code": claude_code.installed(),
        "agent_budget_usd": AGENT_BUDGET_USD,
        "smtp": bool(SMTP_HOST and SMTP_FROM),
        "imap": bool(IMAP_HOST and IMAP_USER),
        "hunter": bool(HUNTER_API_KEY),
        "github_token": bool(GITHUB_TOKEN),
        "twilio_lookup": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "smtp_probe": SMTP_PROBE,
        "auth": bool(APP_PASSWORD),
        "local": LOCAL,
        "email_daily_cap": EMAIL_DAILY_CAP,
        "linkedin_daily_cap": LINKEDIN_DAILY_CAP,
        "run_interval_hours": RUN_INTERVAL_HOURS,
        "research_per_run": RESEARCH_PER_RUN,
        "postal_address": bool(POSTAL_ADDRESS),
    }
