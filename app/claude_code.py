"""Run the agents through Claude Code, signed in to your Claude subscription, instead of an API key.

Each agent run becomes one `claude -p` call. The agent's own tools (save_people, add_companies and the rest) are
offered to Claude Code as an MCP server: a small bridge process (cc_bridge.py) that Claude Code starts, which
passes each tool call back here over a local socket, so the tools still run inside Signalpipe with its database.
Web search and web fetch are Claude Code's own WebSearch and WebFetch tools. Nothing else is allowed: no shell,
no file edits.

What to know:
- Usage counts against your plan's limits (the 5-hour and weekly ones you share with claude.ai), not dollars.
  When you hit a limit, agents stop with an error until it resets; the dashboard keeps working.
- Claude Code must be installed and signed in on the same computer as Signalpipe: run `claude` once in a
  terminal and sign in with your Claude account, or paste a token from `claude setup-token` into Settings.
- An Anthropic API key in the environment would take precedence over your subscription, so it is removed from
  the environment Claude Code runs in.
"""
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile

from . import config

log = logging.getLogger("signalpipe.claude_code")
BRIDGE = os.path.join(os.path.dirname(__file__), "cc_bridge.py")
TIMEOUT = 900
_server: asyncio.AbstractServer | None = None
_port = 0
_runs: dict[str, dict] = {}
_slots = asyncio.Semaphore(2)  # two agent runs at a time is plenty, and gentle on plan limits
last_error = ""


def binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "~/.claude/local/claude", "/usr/local/bin/claude"):
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def installed() -> bool:
    return binary() is not None


_auth_cache: tuple[float, dict] = (0.0, {})


async def auth_status(fresh: bool = False) -> dict:
    """Whether Claude Code is installed and signed in, and whether that sign-in is a Claude plan (not an API key).
    Runs `claude auth status`, which reads local state only and uses no usage. Cached for a minute."""
    import time
    global _auth_cache
    if not fresh and time.time() - _auth_cache[0] < 60:
        return _auth_cache[1]
    exe = binary()
    out = {"installed": bool(exe), "signed_in": False, "subscription": False, "method": ""}
    if exe:
        try:
            proc = await asyncio.create_subprocess_exec(exe, "auth", "status", "--json", env=_env(),
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            raw, _ = await asyncio.wait_for(proc.communicate(), 15)
            data = json.loads(raw.decode() or "{}")
            method = str(data.get("authMethod") or "")
            out.update(signed_in=bool(data.get("loggedIn")), method=method,
                       subscription=bool(data.get("loggedIn")) and "api" not in method.lower()
                       and str(data.get("apiProvider") or "firstParty") == "firstParty")
        except Exception as e:  # an older Claude Code without `auth status`: let the first run say
            log.info("claude auth status unavailable: %s", e)
            out.update(signed_in=None, subscription=None)
    _auth_cache = (time.time(), out)
    return out


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One request per connection from the bridge: {"token", "op": "list"|"call", "name", "args"}."""
    try:
        req = json.loads(await reader.readline())
        run = _runs.get(req.get("token") or "")
        if run is None:
            resp = {"error": "This agent run has ended."}
        elif req.get("op") == "list":
            resp = {"tools": [{"name": t.name, "description": d.get("description", ""), "inputSchema": d["input_schema"]}
                              for t in run["tools"].values() for d in [t.to_dict()]]}
        else:
            name, args = req.get("name"), req.get("args") or {}
            tool = run["tools"].get(name)
            if tool is None:
                text, err = f"Error: there is no tool called {name}.", True
            else:
                try:
                    out = await tool.func(**args)
                    text, err = (out if isinstance(out, str) else json.dumps(out, default=str)), False
                except TypeError as e:
                    text, err = f"Error: wrong arguments for {name}: {e}", True
                except Exception as e:
                    log.warning("tool %s failed: %s", name, e)
                    text, err = f"Error: {type(e).__name__}: {str(e)[:300]}", True
            run["steps"].append({"tool": name, "input": json.dumps(args, default=str)[:160], "result": text[:200], "error": err})
            resp = {"text": text, "is_error": err}
        writer.write((json.dumps(resp, default=str) + "\n").encode())
        await writer.drain()
    except Exception as e:
        log.warning("bridge request failed: %s", e)
    finally:
        writer.close()


async def _ensure_server() -> int:
    global _server, _port
    if _server is None:
        _server = await asyncio.start_server(_handle, "127.0.0.1", 0, limit=4 * 1024 * 1024)
        _port = _server.sockets[0].getsockname()[1]
    return _port


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")}
    token = config.env("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _friendly(text: str) -> str:
    low = text.lower()
    if "login" in low or "log in" in low or "not authenticated" in low or "invalid api key" in low or "oauth" in low:
        return ("Claude Code is not signed in. Open a terminal, run `claude`, and sign in with your Claude account "
                "(or put a token from `claude setup-token` in Settings).")
    if "limit" in low and ("usage" in low or "reset" in low or "rate" in low):
        return "Your Claude plan's usage limit is reached; agents resume when it resets. " + text[:200]
    return text[:400]


async def run(*, system: str, prompt: str, tools: list | None = None, web: bool = False, json_schema: dict | None = None,
              resume: str | None = None, timeout: int = TIMEOUT) -> dict:
    """One `claude -p` run. Returns {ok, text, structured, steps, session_id, usd_estimate, searches, error}."""
    global last_error
    exe = binary()
    if not exe:
        return {"ok": False, "error": "Claude Code is not installed on this computer. See Settings for the one-line install.",
                "steps": []}
    auth = await auth_status()
    if auth.get("signed_in") is False:
        last_error = _friendly("not signed in, please log in")
        return {"ok": False, "error": last_error, "steps": []}
    if auth.get("subscription") is False:  # signed in with a Console account or key: that would bill the API
        last_error = ("Claude Code is signed in with an API account, which would bill API usage. Run `claude auth logout`, "
                      "then `claude` and sign in with your Claude Pro or Max account.")
        return {"ok": False, "error": last_error, "steps": []}
    tools = tools or []
    token = secrets.token_urlsafe(24)
    _runs[token] = {"tools": {t.name: t for t in tools}, "steps": []}
    builtin = ["WebSearch", "WebFetch"] if web else []
    args = [exe, "-p", "--output-format", "json", "--model", config.LLM_MODEL, "--system-prompt", system,
            "--tools", ",".join(builtin), "--permission-mode", "dontAsk", "--strict-mcp-config",
            "--setting-sources", "project"]
    allowed = builtin + (["mcp__signalpipe"] if tools else [])
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
    if json_schema:
        args += ["--json-schema", json.dumps(json_schema)]
    if resume:
        args += ["--resume", resume]
    try:
        async with _slots:
            with tempfile.TemporaryDirectory(prefix="signalpipe-cc-") as tmp:
                if tools:
                    port = await _ensure_server()
                    cfg = os.path.join(tmp, "mcp.json")
                    with open(cfg, "w") as f:
                        json.dump({"mcpServers": {"signalpipe": {"type": "stdio", "command": sys.executable,
                                                                 "args": ["-I", BRIDGE, str(port), token]}}}, f)
                    args += ["--mcp-config", cfg]
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=tmp, env=_env(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return {"ok": False, "error": f"Claude Code took longer than {timeout // 60} minutes.",
                            "steps": _runs[token]["steps"]}
        steps = _runs[token]["steps"]
        try:
            data = json.loads(out.decode() or "{}")
        except ValueError:
            data = {}
        if not data:
            last_error = _friendly((err.decode() or out.decode() or f"Claude Code exited with code {proc.returncode}").strip())
            return {"ok": False, "error": last_error, "steps": steps}
        stu = (data.get("usage") or {}).get("server_tool_use") or {}
        res = {"ok": not data.get("is_error") and data.get("subtype") == "success", "text": data.get("result") or "",
               "structured": data.get("structured_output"), "steps": steps, "session_id": data.get("session_id"),
               "usd_estimate": float(data.get("total_cost_usd") or 0), "searches": int(stu.get("web_search_requests") or 0)}
        if not res["ok"]:
            res["error"] = last_error = _friendly(data.get("result") or str(data.get("subtype") or "Claude Code failed"))
        else:
            last_error = ""
        return res
    finally:
        _runs.pop(token, None)


async def ask_json(system: str, prompt: str, schema: dict) -> dict | None:
    res = await run(system=system, prompt=prompt, json_schema=schema, timeout=300)
    if not res.get("ok"):
        return None
    if isinstance(res.get("structured"), dict):
        return res["structured"]
    try:
        return json.loads(res.get("text") or "")
    except ValueError:
        return None
