"""MCP server for Signalpipe: review leads and run plays from Claude or any MCP client.

It talks to a running Signalpipe over its HTTP API, so point it at your deployment:

    SIGNALPIPE_URL=https://your-host  SIGNALPIPE_TOKEN=<API_TOKEN>  python mcp_server.py
"""
import os

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("SIGNALPIPE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("SIGNALPIPE_TOKEN", "")
mcp = MCPServer("signalpipe")


def _call(method: str, path: str, body: dict | None = None, params: dict | None = None):
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    r = httpx.request(method, f"{BASE}/api{path}", json=body, params=params, headers=headers, timeout=120)
    if r.status_code >= 400:
        return {"error": r.json().get("error", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]}
    return r.json()


@mcp.tool()
def pipeline_status() -> dict:
    """Lead counts by status, what is connected, and today's send totals."""
    return _call("GET", "/status")


@mcp.tool()
def list_leads(status: str = "new", min_score: float = 0, limit: int = 25) -> dict:
    """Leads, best first. status: new, approved, contacted, replied, won, lost, rejected or all."""
    data = _call("GET", "/leads", params={"status": status, "min_score": min_score, "limit": limit})
    if "leads" in data:
        data["leads"] = [{k: l.get(k) for k in ("id", "company_name", "company_domain", "score", "fit", "intent",
                                                 "top_signal", "top_title", "contact", "has")} |
                         {"why": (l.get("reasons") or {}).get("items", [])[:3]} for l in data["leads"]]
    return data


@mcp.tool()
def get_lead(lead_id: int) -> dict:
    """One lead in full: signals, contacts with email and phone validation, drafted messages, replies."""
    return _call("GET", f"/leads/{lead_id}")


@mcp.tool()
def approve_lead(lead_id: int, note: str = "") -> dict:
    """Approve a lead and draft its email, LinkedIn and call sequence. Nothing is sent until each step is approved."""
    return _call("POST", f"/leads/{lead_id}/approve", {"note": note or None})


@mcp.tool()
def reject_lead(lead_id: int, note: str = "") -> dict:
    """Reject a lead. The note teaches future scoring what you do not want."""
    return _call("POST", f"/leads/{lead_id}/reject", {"note": note or None})


@mcp.tool()
def list_plays() -> list | dict:
    """All plays with their config and last run."""
    return _call("GET", "/plays")


@mcp.tool()
def run_play(play_id: int) -> dict:
    """Collect new signals for a play now, then enrich and score the leads. Takes up to a minute."""
    return _call("POST", f"/plays/{play_id}/run", {"wait": True})


@mcp.tool()
def outreach_queue(channel: str = "email") -> dict:
    """Steps waiting for review or sending. channel: email, linkedin or call."""
    return _call("GET", f"/outreach/{channel}")


@mcp.tool()
def edit_message(message_id: int, subject: str = "", body: str = "", approve: bool = False) -> dict:
    """Rewrite a drafted step, and optionally approve it for sending."""
    payload = {k: v for k, v in (("subject", subject), ("body", body)) if v}
    if approve:
        payload["action"] = "queue"
    return _call("PUT", f"/messages/{message_id}", payload)


@mcp.tool()
def inbox() -> list | dict:
    """Unhandled replies, each with a drafted answer."""
    return _call("GET", "/replies", params={"handled": 0})


@mcp.tool()
def check_email(email: str) -> list | dict:
    """Validate one email address."""
    return _call("POST", "/validate/email", {"email": email})


@mcp.tool()
def check_phone(phone: str, region: str = "") -> list | dict:
    """Validate one phone number; returns line type, region, carrier and whether it is in business hours."""
    return _call("POST", "/validate/phone", {"phone": phone, "region": region})


@mcp.tool()
def seo_audit(url: str = "", max_pages: int = 30) -> dict:
    """Audit a site for search health and get a content plan. Defaults to the site in the Brain."""
    data = _call("POST", "/seo/audit", {"url": url, "max_pages": max_pages})
    data.pop("pages", None)
    return data


if __name__ == "__main__":
    mcp.run()
