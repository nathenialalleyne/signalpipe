"""Run Signalpipe on this computer: `python -m app.local` (the start-mac / start-windows / start-linux launchers do this).

Everything lives in ~/.signalpipe: the database, the LinkedIn agent's Chrome profile and its log. The app
listens on this computer only, and the browser is signed in automatically with a key kept in that folder.
Starting it again while it is running just opens the browser.
"""
import argparse
import json
import os
import pathlib
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SIGNALPIPE_HOME") or (pathlib.Path.home() / ".signalpipe"))


def _identity(home: pathlib.Path) -> dict:
    path = home / "local.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    changed = False
    for k, n in (("key", 24), ("secret", 32), ("token", 24)):
        if not data.get(k):
            data[k] = secrets.token_urlsafe(n)
            changed = True
    if changed:
        _save(path, data)
    return data


def _save(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _local_get(url: str, timeout: float = 2.0) -> bytes | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send this through a proxy
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _already_running(port: int | None) -> bool:
    if not port:
        return False
    body = _local_get(f"http://127.0.0.1:{port}/api/mode")
    try:
        return bool(body and json.loads(body).get("local"))
    except ValueError:
        return False


def _free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port found between %d and %d." % (preferred, preferred + 40))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Signalpipe on this computer.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args()

    home = _home()
    ident = _identity(home)
    if _already_running(ident.get("port")):
        url = f"http://127.0.0.1:{ident['port']}/api/local-login?key={ident['key']}"
        print("Signalpipe is already running. Opening it in your browser.")
        if not args.no_browser:
            webbrowser.open(url)
        return

    port = _free_port(args.port or 8777)
    ident["port"] = port
    _save(home / "local.json", ident)
    os.environ.update({
        "SIGNALPIPE_LOCAL": "1", "SIGNALPIPE_HOME": str(home), "SIGNALPIPE_DATA": str(home / "data"),
        "SIGNALPIPE_PORT": str(port), "APP_PASSWORD": ident["key"], "SECRET_KEY": ident["secret"],
        "API_TOKEN": ident["token"], "COOKIE_SECURE": "0",
    })

    import uvicorn  # imported only now: the app reads its settings from the environment when it loads

    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    url = f"http://127.0.0.1:{port}/api/local-login?key={ident['key']}"

    def announce() -> None:
        while not server.started:
            time.sleep(0.2)
        print(f"\n  Signalpipe is running at http://127.0.0.1:{port}\n"
              "  Keep this window open while you use it. Close it (or press Ctrl+C) to stop.\n"
              "  To open it again later, start Signalpipe the same way.\n", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    server.run()


if __name__ == "__main__":
    sys.exit(main())
