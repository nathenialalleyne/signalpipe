"""MCP server (stdio) that Claude Code starts for one agent run. It forwards tool calls to Signalpipe.

Usage: python -I cc_bridge.py <port> <token>. Standard library only, and it imports nothing from the app, so it
starts fast and has no side effects. Messages are newline-delimited JSON-RPC 2.0, per the MCP stdio transport.
"""
import json
import socket
import sys

PORT, TOKEN = int(sys.argv[1]), sys.argv[2]


def ask(req: dict) -> dict:
    with socket.create_connection(("127.0.0.1", PORT), timeout=600) as s:
        s.sendall((json.dumps({**req, "token": TOKEN}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:  # a notification; nothing to answer
            continue
        try:
            if method == "initialize":
                reply(msg_id, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                               "capabilities": {"tools": {}}, "serverInfo": {"name": "signalpipe", "version": "1.0"}})
            elif method == "tools/list":
                r = ask({"op": "list"})
                reply(msg_id, {"tools": r.get("tools", [])})
            elif method == "tools/call":
                r = ask({"op": "call", "name": params.get("name"), "args": params.get("arguments") or {}})
                text = r.get("text") if "text" in r else r.get("error", "Signalpipe did not answer.")
                reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": bool(r.get("is_error") or "error" in r)})
            elif method == "ping":
                reply(msg_id, {})
            else:
                reply(msg_id, error={"code": -32601, "message": f"Unknown method {method}"})
        except Exception as e:  # keep the server alive; report the failure to Claude
            reply(msg_id, error={"code": -32000, "message": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
