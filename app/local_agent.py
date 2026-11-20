"""When Signalpipe runs on the person's own computer, it starts the LinkedIn agent itself.

The agent opens its own Chrome window (a separate profile, so your everyday Chrome is untouched), on your
computer and your connection. It runs with the Node runtime and browser library that ship inside the Python
Playwright package, so nothing else needs installing. It is restarted if it stops, and it closes its
browser when Signalpipe quits.
"""
import asyncio
import logging
import os
import pathlib
import subprocess
import threading
import time

from . import config, db

log = logging.getLogger("signalpipe.local_agent")
_proc: subprocess.Popen | None = None
_task: asyncio.Task | None = None
_lock = threading.Lock()


def home() -> pathlib.Path:
    return pathlib.Path(config.env("SIGNALPIPE_HOME") or (pathlib.Path.home() / ".signalpipe"))


def script() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "agent" / "linkedin-agent.mjs"


def runtime() -> tuple[str, str]:
    """(node executable, playwright-core directory) from the installed Python Playwright package."""
    try:
        from playwright._impl._driver import compute_driver_executable
        node, cli = compute_driver_executable()
        return str(node), str(pathlib.Path(cli).parent)
    except Exception:
        import playwright
        driver = pathlib.Path(playwright.__file__).parent / "driver"
        return str(driver / ("node.exe" if os.name == "nt" else "node")), str(driver / "package")


def wanted() -> bool:
    return bool(db.get_setting("linkedin_agent_on", False))


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    return {"available": config.LOCAL, "on": wanted(), "running": running(),
            "log": str(home() / "linkedin-agent.log")}


def _env() -> dict:
    node, core = runtime()
    e = dict(os.environ)
    e.update({
        "SIGNALPIPE_URL": f"http://127.0.0.1:{config.env('SIGNALPIPE_PORT', '8777')}",
        "SIGNALPIPE_TOKEN": config.API_TOKEN,
        "PLAYWRIGHT_CORE": core,
        "LINKEDIN_SERVER": "1",        # never exit: when LinkedIn needs you, wait in the window
        "LINKEDIN_LOCAL": "1",
        "LINKEDIN_PARENT_PIPE": "1",   # close the browser when Signalpipe quits
        "LINKEDIN_MODE": "attach",
        "LINKEDIN_PROFILE_DIR": str(home() / "chrome-profile"),
        "LINKEDIN_ERROR_DIR": str(home() / "errors"),
    })
    for k in ("ANTHROPIC_API_KEY", "SMTP_PASS", "IMAP_PASS", "HUNTER_API_KEY", "APP_PASSWORD", "SECRET_KEY"):
        e.pop(k, None)  # the agent needs none of these
    return e


def _spawn() -> subprocess.Popen:
    node, _ = runtime()
    home().mkdir(parents=True, exist_ok=True)
    logfile = open(home() / "linkedin-agent.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no extra console window on Windows
    return subprocess.Popen([node, str(script()), "run", "--server"], stdin=subprocess.PIPE, stdout=logfile,
                            stderr=subprocess.STDOUT, env=_env(), cwd=str(script().parent), creationflags=flags)


async def _supervise() -> None:
    global _proc
    delay = 10
    while wanted():
        started = time.time()
        with _lock:
            _proc = _spawn()
        log.info("LinkedIn agent started (pid %s)", _proc.pid)
        code = await asyncio.to_thread(_proc.wait)
        if not wanted():
            break
        log.warning("LinkedIn agent exited with %s; restarting in %ss", code, delay)
        delay = 10 if time.time() - started > 300 else min(delay * 2, 600)
        await asyncio.sleep(delay)


def start() -> None:
    global _task
    if not config.LOCAL:
        return
    db.set_setting("linkedin_agent_on", True)
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_supervise())


def _close_proc(timeout: float = 10) -> None:
    with _lock:
        p = _proc
    if p is None or p.poll() is not None:
        return
    try:
        p.stdin.close()  # the agent sees this, closes its browser and exits
    except Exception:
        pass
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


async def stop(remember: bool = True) -> None:
    """Close the agent and its browser. remember=False is for quitting Signalpipe: it starts again next time."""
    global _task
    if remember:
        db.set_setting("linkedin_agent_on", False)
    task, _task = _task, None
    if task:
        task.cancel()
    await asyncio.to_thread(_close_proc)
