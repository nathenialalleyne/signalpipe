"""End to end: the real Signalpipe server, the real agent in a real (headless) browser, and a stand-in
LinkedIn site. Proves the agent's logic, pacing rules and protocol. It cannot prove that LinkedIn's own
pages match the stand-in; that needs a run against a real account.
"""
import os
import shutil
import subprocess
import sys
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE, APP = 8931, 8932
H = {"Authorization": "Bearer tok"}

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode != 0,
    reason="needs node with playwright")


def wait_http(url, tries=60):
    for _ in range(tries):
        try:
            httpx.get(url, timeout=2)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    env = {**os.environ, "SIGNALPIPE_DATA": str(tmp / "data"), "API_TOKEN": "tok", "APP_PASSWORD": "pw",
           "SIGNALPIPE_LLM": "0", "SIGNALPIPE_SCHEDULER": "0", "LINKEDIN_HOURS": "0-24", "LINKEDIN_WEEKENDS": "1",
           "LINKEDIN_MIN_GAP_MIN": "0", "LINKEDIN_MAX_GAP_MIN": "0", "LINKEDIN_CHECK_MIN": "0.05",
           "LINKEDIN_FOLLOWUP_DELAY_HOURS": "0-0", "UNIPILE_DSN": "", "UNIPILE_API_KEY": ""}
    fake = subprocess.Popen(["node", os.path.join(ROOT, "tests", "fake_linkedin.mjs"), str(FAKE)], stdout=subprocess.DEVNULL)
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(APP), "--log-level", "warning"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_http(f"http://127.0.0.1:{FAKE}/_state")
        wait_http(f"http://127.0.0.1:{APP}/healthz")
        yield {"profile": str(tmp / "chrome"), "errors": str(tmp / "errors")}
    finally:
        fake.terminate()
        app.terminate()


def api(method, path, **kw):
    r = httpx.request(method, f"http://127.0.0.1:{APP}/api{path}", headers=H, timeout=60, **kw)
    assert r.status_code < 400, (path, r.status_code, r.text[:300])
    return r.json()


def fake_state():
    return httpx.get(f"http://127.0.0.1:{FAKE}/_state").json()


def agent(stack, *args, timeout=150):
    env = {**os.environ, "SIGNALPIPE_URL": f"http://127.0.0.1:{APP}", "SIGNALPIPE_TOKEN": "tok",
           "LINKEDIN_BASE": f"http://127.0.0.1:{FAKE}", "LINKEDIN_CHANNEL": "", "LINKEDIN_HEADLESS": "1",
           "LINKEDIN_SPEED": "0.02", "LINKEDIN_PROFILE_DIR": stack["profile"], "LINKEDIN_ERROR_DIR": stack["errors"],
           "LINKEDIN_EMAIL": "me@example.com", "LINKEDIN_PASSWORD": "correct-horse"}
    return subprocess.run(["node", os.path.join(ROOT, "agent", "linkedin-agent.mjs"), *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def import_and_approve(play_id, rows):
    csv = "Name,Company,Position,URL,Website\n" + "\n".join(rows) + "\n"
    api("POST", f"/plays/{play_id}/import", content=csv)
    ids = []
    for lead in api("GET", "/leads", params={"status": "new", "play_id": play_id})["leads"]:
        if lead["company_domain"].endswith(".invalid"):
            api("POST", f"/leads/{lead['id']}/approve", json={})
            ids.append(lead["id"])
    return ids


def linkedin_steps(status):
    return {(m["contact_name"], m["step"]): m for m in api("GET", "/outreach/linkedin", params={"status": status})["items"]}


def test_agent_runs_linkedin_end_to_end(stack):
    api("PUT", "/brain", json={"company": "Sender Co", "one_liner": "We fix outbound.", "sender_name": "Sam",
                               "pains": ["Reps waste time prospecting"], "icp": {"titles": ["Head of Sales"]}})
    play = api("POST", "/plays", json={"name": "LinkedIn", "config": {
        "sources": [], "channels": ["linkedin"], "keywords": ["cold email"],
        "linkedin_watch": ["https://www.linkedin.com/company/hubspot/"]}})
    pid = play["id"]

    assert not api("GET", "/status")["linkedin"]["enabled"]
    assert api("GET", "/agent/linkedin/work")["kind"] == "idle"          # first check-in: the agent now counts as connected
    st = api("GET", "/status")["linkedin"]
    assert st["enabled"] and st["via"] == "agent"
    api("PUT", "/settings", json={"send_mode": "auto"})
    api("PUT", f"/plays/{pid}", json={"config": {"sources": ["linkedin"]}})

    import_and_approve(pid, [
        "Dana Reyes,Acme,Head of Sales,https://www.linkedin.com/in/dana-x,acme.invalid",
        "Lee Park,Beta Corp,CEO,https://www.linkedin.com/in/more-menu,betacorp.invalid",
        "Ben Ode,Initech,Founder,https://www.linkedin.com/in/ben-first,initech.invalid"])
    queued = linkedin_steps("open")
    assert queued[("Dana Reyes", 1)]["status"] == "queued"               # hands-off: no per-step approval needed

    # --- first pass: sign in with saved credentials, send invites, follow up with an existing connection, read pages
    run = agent(stack, "run", "--max-work", "12", "--exit-on-idle")
    assert run.returncode == 0, run.stdout[-1500:] + run.stderr[-800:]
    fs = fake_state()
    assert fs["logins"] == 1
    invited = {i["slug"]: i["note"] for i in fs["invites"]}
    assert set(invited) == {"dana-x", "more-menu"}                       # Connect found on the card and in the More menu
    assert invited["dana-x"] == queued[("Dana Reyes", 1)]["body"] and len(invited["dana-x"]) <= 200
    assert "decoy" not in fs                                             # never clicked "People you may know"
    done = linkedin_steps("done")
    assert done[("Dana Reyes", 1)]["outcome"] == "invited:agent"
    assert done[("Ben Ode", 1)]["outcome"] == "accepted"                 # already a connection: no invite sent
    assert done[("Ben Ode", 2)]["outcome"] == "chat:agent"
    assert [m["slug"] for m in fs["messages"]] == ["ben-first"]
    assert fs["messages"][0]["text"] == done[("Ben Ode", 2)]["body"]
    assert ("Dana Reyes", 2) in linkedin_steps("open")                   # follow-up waits until she accepts
    found = {l["company_name"]: l for l in api("GET", "/leads", params={"status": "new"})["leads"]}
    assert {"Globex", "Vandelay"} <= set(found)                          # commenter and keyword-post author became leads
    globex = api("GET", f"/leads/{found['Globex']['id']}")
    assert globex["contacts"][0]["name"] == "Ana Ruiz" and globex["contacts"][0]["linkedin_url"].endswith("/in/ana-ruiz")
    assert globex["signals"][0]["type"] == "competitor_engagement" and "pricing" in globex["signals"][0]["text"]
    assert not any("Agency" in n or "Some Page" in n for n in found)     # company pages are not people

    # --- second pass: Dana accepts, Ben replies
    httpx.get(f"http://127.0.0.1:{FAKE}/_accept", params={"slug": "dana-x"})
    httpx.get(f"http://127.0.0.1:{FAKE}/_reply", params={"slug": "ben-first", "text": "Happy to chat next week"})
    time.sleep(3.2)                                                      # let the (shortened) check interval pass
    run = agent(stack, "run", "--max-work", "10", "--exit-on-idle")
    assert run.returncode == 0, run.stdout[-1500:] + run.stderr[-800:]
    fs = fake_state()
    assert fs["logins"] == 1                                             # saved session reused; no second sign-in
    assert [m["slug"] for m in fs["messages"]] == ["ben-first", "dana-x"]
    done = linkedin_steps("done")
    assert done[("Dana Reyes", 1)]["outcome"] == "accepted" and done[("Dana Reyes", 2)]["outcome"] == "chat:agent"
    replied = api("GET", "/leads", params={"status": "replied"})["leads"]
    assert [l["company_name"] for l in replied] == ["Initech"]
    inbox = api("GET", "/replies")
    assert len(inbox) == 1 and inbox[0]["body"] == "Happy to chat next week"   # only their words, not our own message

    # --- LinkedIn shows a security check: the agent stops and says so, and does not try to get past it
    [cp] = import_and_approve(pid, ["Che Point,Gate Co,VP Sales,https://www.linkedin.com/in/checkpoint-user,gate.invalid"])
    run = agent(stack, "run", "--max-work", "3", "--exit-on-idle")
    assert run.returncode == 2
    st = api("GET", "/status")["linkedin"]
    assert "checkpoint" in st["agent"]["blocked"] and "agent stopped" in st["error"]
    assert linkedin_steps("open")[("Che Point", 1)]["status"] == "queued"      # nothing was marked as sent
    assert api("GET", "/agent/linkedin/work")["kind"] == "idle"
    assert agent(stack, "login").returncode == 0                               # you sign in again; the agent reports ready
    assert api("GET", "/status")["linkedin"]["agent"]["blocked"] == ""
    api("POST", f"/leads/{cp}/reject", json={})

    # --- LinkedIn says the weekly invitation limit is reached: everything pauses
    import_and_approve(pid, ["Lim Itt,Capped,COO,https://www.linkedin.com/in/limit-user,capped.invalid"])
    run = agent(stack, "run", "--max-work", "3", "--exit-on-idle")
    assert run.returncode == 0, run.stdout[-1500:]
    assert api("GET", "/status")["linkedin"]["paused_until"] > time.time() + 19 * 3600
    assert linkedin_steps("open")[("Lim Itt", 1)]["status"] == "queued"
    assert "limit-user" not in {i["slug"] for i in fake_state()["invites"]}
    assert "paused" in api("GET", "/agent/linkedin/work")["reason"]
