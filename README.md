# Signalpipe

A self-hosted, single-user outbound pipeline: it learns your business from your website, watches public
buying signals, turns them into scored leads with validated contact details, and runs a short sequence
across email, LinkedIn and phone. It also audits your own site for search and plans content from what
prospects are asking.

One container, one SQLite file, no build step.

## How it works

1. **Brain.** Enter your website. Claude extracts what you sell, buyer titles, pains, competitors, pain
   keywords and the job titles whose hiring implies a need for you. Everything is editable.
2. **Plays.** A play is a standing search. Each run collects signals from the sources you tick:
   | Source | What it finds | Access |
   | --- | --- | --- |
   | `jobs` | Companies advertising your hiring-signal roles | RemoteOK, Remotive, Arbeitnow, HN "Who is hiring" |
   | `ats` | Open roles at named target accounts | Greenhouse, Lever, Ashby public job boards |
   | `funding` | New funding rounds | TechCrunch and Google News feeds |
   | `hn` | People weighing or leaving a competitor, or describing your pain | Hacker News search API |
   | `github` | New stargazers of repos you list | GitHub REST API |
   | `linkedin` | Engagers on competitor pages, authors of keyword posts | Unipile (optional, see below) |
   | CSV import | Any list, including LinkedIn's connections export | Upload on the Plays page |
3. **Enrichment.** For each company: resolve the domain, read its own about, team and contact pages,
   and extract named people with titles, published emails, phone numbers, the LinkedIn company page and
   the tools running on the site.
4. **Validation.**
   - Email: syntax, disposable, role and random-looking address detection, MX lookup, mail provider,
     suppression list. With `SMTP_PROBE=1` it also does what paid verifiers do: it asks the address's
     own mail server whether the mailbox exists (without sending anything), detects catch-all domains
     that accept everything, retries once on greylisting, and tells "this mailbox does not exist"
     apart from "this server refused to talk to us". For a named person it tries the common address
     patterns until the server accepts one, remembers that company's pattern, and uses it for
     colleagues. A personal address published on a company's site teaches the pattern the same way.
     If no pattern is a real mailbox it stores no address rather than a wrong one.
     Without probing (or `HUNTER_API_KEY`), a derived address is labelled "risky, pattern guess", or
     "likely, pattern confirmed" when the company's pattern is known. It is never shown as verified.
   - Mailbox probing needs outbound port 25. AWS restricts it on Lightsail and EC2 by default; request
     removal with the "Request to remove email sending limitations" form and ask for reverse DNS on
     the static IP at the same time, then set `SMTP_HELO` to that hostname. Probes are paced one
     conversation per domain. Catch-all domains can never be confirmed by any verifier.
   - Phone: libphonenumber validity, line type, region, carrier, timezone and a live business-hours
     window. `TWILIO_*` adds carrier-grade line type.
5. **Scoring.** `0.5 x fit + 0.3 x intent + 0.2 x recency`. Fit is judged by Claude against your ICP
   and calibrated on your past approve and reject decisions; without a key it falls back to keyword
   matching. A wrong-fit company is capped so it cannot rank on intent alone.
6. **Review.** Approve or reject. Approving drafts three emails, a LinkedIn connection note and
   follow-up, and a call script, all opened on the actual signal.
7. **Outreach.**
   - Email goes out through your own SMTP mailbox with a daily cap, unsubscribe line, postal address
     and `List-Unsubscribe` header. Replies are read over IMAP: a reply stops the sequence and gets a
     drafted answer, a bounce marks the address invalid, an unsubscribe goes on the suppression list.
   - LinkedIn steps wait in a queue with the profile link and note. With Unipile connected they send
     automatically under daily and weekly caps, acceptances release the follow-up, and inbound
     messages stop the sequence.
   - Calls are a dial list sorted by who is in business hours, with outcome logging.
   - In "Review each step" mode nothing sends until you approve it. In "Send automatically" mode the
     first email to a verified address goes out unreviewed, and with LinkedIn connected so does the
     connection request to a known profile, with the follow-up released when they accept. You still
     choose which leads to approve.
8. **Learning.** Three loops run without you doing anything extra:
   - *A/B testing.* Every approved lead gets one of four opening angles for its first email and
     LinkedIn note (lead with the signal, the pain, a question, or proof). The angle is chosen by
     Thompson sampling over past results, so openings that earn replies get most of the traffic while
     untested ones still get tried.
   - *Outcome tracking.* The Insights page shows the funnel and breaks reply, interest, LinkedIn
     acceptance and wins down by opening, signal type, buyer level, score band and channel. A group
     needs 10 contacted leads before its rate is treated as meaningful.
   - *ICP feedback.* Scoring is calibrated on your approve and reject decisions and, more heavily, on
     who actually replied with interest and who went quiet.
9. **SEO.** Crawls your site (robots, sitemap, titles, descriptions, headings, canonicals, thin
   pages, duplicates, broken links, structured data, llms.txt), checks which buyer phrases have no
   page, and writes content briefs from those gaps and from prospect questions in your signals. It does
   not report search volume, rankings or backlinks; connect Search Console for those.

## Agents

Eight agents share one engine (`app/agentkit.py`): Claude gets a set of tools, uses them, sees the
results and keeps going until the job is done. Every run is logged on the **Agents** page with each step
it took and what it cost, each agent can be switched off there, and a monthly cap in Settings stops all
of them when API spend reaches it.

| Agent | What it does | When it runs |
| --- | --- | --- |
| Prospector | Searches the web for companies that fit your profile and show a trigger now, plus lookalikes of leads you approved and won, skipping ones you rejected | Play runs with the `agent` source |
| People finder | Finds the named buyers at a company, with LinkedIn profiles it actually saw and work emails it checked; records why each is the right person and hooks for the message | Best new leads after each play run, and on approve |
| Writer | Writes the sequence from the evidence, may read the page behind a signal, and has every draft checked by code (length, no exclamation marks or em dashes, no stock phrases, the LinkedIn note limit); failing drafts are refused until fixed | On approve |
| Inbox | Handles a reply by acting on it: schedules the follow-up for "try me in March", adds the person you were referred to with an intro draft, records a meeting or a no, honours an unsubscribe, and asks you what your profile cannot answer instead of guessing | Each reply |
| Re-engagement | When a lead that went quiet three weeks ago shows a new signal, drafts a message about it | Every 30 minutes, if any |
| Strategist | Reads a week of results and proposes changes to keywords, competitors, buyer titles, disqualifiers and messaging, which you apply on the Insights page | Weekly |
| Content writer | Turns a brief on the SEO page into a researched article draft with sources | When you press Write the draft |
| Copilot | A chat on the Agents page that runs the rest: "find 15 logistics companies that raised money this month", "research the top five", "why are replies low?" | When you ask |

Nothing an agent does sends a message: drafts wait in Outreach under your sending rules.

### Use your Claude subscription instead of an API key

On your own computer this is the default: Signalpipe runs Claude through
[Claude Code](https://code.claude.com/docs) signed in to your Pro or Max plan, so there is no separate
bill. Install Claude Code once on the same computer (inside WSL on Windows), then run `claude` and sign
in with your Claude account:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Restart Signalpipe so it finds it; **Settings** shows whether Claude Code is signed in to your plan.
If Claude Code is signed in with a Console (API) account instead, the agents refuse to run rather than
bill the API. An API key is still available under Settings for servers, but nothing uses it unless you
switch to it. How it works: each agent run is one `claude -p` call; the agent's own
tools are offered to Claude Code as a small MCP server (`app/cc_bridge.py`) that passes each call back to
Signalpipe, and web research uses Claude Code's own WebSearch and WebFetch. Nothing else is allowed (no
shell, no file access). Any `ANTHROPIC_API_KEY` is removed from Claude Code's environment so it cannot
bill the API instead.

Things to know:
- Usage counts toward your plan's limits, the same 5-hour and weekly ones you share with claude.ai and
  Claude Code. Heavy play runs with many researched leads can use a lot; lower "Leads the People finder
  researches per play run" if you hit them. When a limit is reached, agents stop with that message and
  the rest of the dashboard keeps working.
- Anthropic allows signing in with your subscription for your own use of Claude Code. This is a
  single-user tool you run yourself, which fits that; it would not be right to offer it to other people
  on your login.
- On a server, run `claude setup-token` where you are signed in and paste the token into Settings.
  The Docker image does not include Claude Code, so the subscription option is meant for running locally.

## Run it

### On your own computer (recommended)

1. On the repository's GitHub page, press **Code**, then **Download ZIP**, and unzip it anywhere.
2. Start it:
   - **Mac**: double-click `start-mac.command`. The first time, macOS may say it is from an unidentified
     developer: right-click it, choose **Open**, then **Open** again.
   - **Windows**: double-click `start-windows.bat`. If Windows says it protected your PC, choose
     **More info**, then **Run anyway**.
   - **Linux**: run `./start-linux.sh`.

   The first start installs [uv](https://docs.astral.sh/uv/) (which sets up the right Python) and
   Signalpipe's packages, then opens Signalpipe in your browser. Later starts take seconds.
3. Install Claude Code and sign in with your Claude account so the agents run on your subscription
   (see [Agents](#agents)). In **Settings**, type your email address (the server settings fill in for
   known providers) and an app password, press **Save and test**, then press **Connect LinkedIn** and
   sign in once in the Chrome window that opens.
4. In **Brain**, enter your website. In **Plays**, create a play and press **Run now**.

Keep the launcher's window open while you use Signalpipe; close it to stop. Starting it again while it
runs just opens the browser. Plays run, emails go out and LinkedIn steps happen only while it is
running and your computer is awake.

Everything stays on your computer in `~/.signalpipe`: the database (including the keys you enter in
Settings), the LinkedIn agent's Chrome profile and its log. The app only answers on this computer, and
the browser is signed in with a key from that folder. LinkedIn sees your normal home or office
connection, the same one you use LinkedIn from yourself. It needs Chrome, Edge or Chromium installed.

To update, download the ZIP again (or `git pull`) and start it the same way; your data is untouched.

If the Mac launcher will not open at all, open Terminal in that folder and run `bash start-linux.sh`.

**On Windows with WSL**: run `./start-linux.sh` in your WSL terminal and open the link it prints in
your Windows browser. For LinkedIn, the agent needs a Chrome inside WSL; Windows 11's WSLg shows its
window on your desktop. Install it once:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
```

```bash
sudo apt install -y ./google-chrome-stable_current_amd64.deb
```

### On a server

```bash
cp .env.example .env
```

Fill in `.env` (at minimum `APP_PASSWORD`, `SECRET_KEY`, `DOMAIN`, `ANTHROPIC_API_KEY`), then:

```bash
docker compose up -d --build
```

Caddy issues the HTTPS certificate for `DOMAIN`. Data lives in the `data` volume; back up
`/data/signalpipe.db`.

### On AWS Lightsail

1. In the Lightsail console create an instance: Linux, **Ubuntu 24.04**, the **2 GB** plan (the
   LinkedIn agent runs a browser; 1 GB works with swap but is tight). Under **Add launch script**,
   paste the contents of
   [`deploy/lightsail-launch.sh`](deploy/lightsail-launch.sh). To use your own domain, fill in
   `DOMAIN=""` at the top of the script first.
2. On the instance's **Networking** tab, attach a static IP and add a firewall rule for **HTTPS (443)**.
   Port 80 must stay open too; it is used to issue the certificate.
3. After about ten minutes, open the browser SSH terminal and read the address and generated password:

   ```bash
   sudo cat /root/signalpipe-info.txt
   ```

4. Add your keys to `/opt/signalpipe/.env` (see `.env.example`), then apply them:

   ```bash
   cd /opt/signalpipe && sudo docker compose up -d
   ```

Without your own domain the script uses a `<ip>.sslip.io` hostname so a certificate can be issued. If
the page does not load over HTTPS after ten minutes, check `/var/log/signalpipe-setup.log` and
`sudo docker compose logs caddy`; the usual cause is port 443 still closed, and the fallback is to
point a domain you own at the static IP and set `DOMAIN` in `.env`.

Local development:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/uvicorn app.main:app --reload
```

```bash
.venv/bin/python -m pytest tests -q
```

## LinkedIn

LinkedIn has no public API for reading post engagers or sending invitations. Tools that offer this
operate your account as you. There are three ways to do the same here; without any of them, LinkedIn
steps wait in a queue for you to send by hand.

### Option 1: the agent on the server (free, always on; the default)

`docker compose` starts the agent in its own container next to the app (`agent/Dockerfile`). It runs
Chromium as a normal window on a virtual display and works through the queue around the clock, within
your working hours and caps.

- **Signing in.** Open **Settings**, then **Open the agent's screen**: that is the agent's browser
  window, in your browser. Sign in to LinkedIn there once, including any code LinkedIn sends you. The
  session is kept in the `agent-profile` volume, so restarts and reboots do not sign it out. To let it
  sign itself back in when a session expires, add `LINKEDIN_EMAIL` and `LINKEDIN_PASSWORD` to `.env`
  (wrap a password containing `$` in single quotes) and run `sudo docker compose up -d`.
- **Security checks.** If LinkedIn shows one, the agent stops, the dashboard says it needs you, and
  the step goes back in the queue. Open the agent's screen and complete the check; the agent notices
  within about 20 seconds and carries on. It never tries to complete a check itself.
- **Access.** Only people signed in to Signalpipe can open the screen: the web server asks the app to
  confirm the session before passing anything through (`deploy/Caddyfile`).
- **The biggest risk is the address.** On the server, LinkedIn sees your account active from an AWS
  data-centre IP, not from your home or office. That is a strong signal and the most likely reason
  for a security check, especially at first sign-in. The agent does not hide or change its address.
  If that matters more to you than always-on, use Option 2 instead and stop the server agent with
  `sudo docker compose stop agent`.

Check what it sees on a real profile without doing anything:

```bash
cd /opt/signalpipe && sudo docker compose exec agent node linkedin-agent.mjs doctor https://www.linkedin.com/in/someone
```

Its log is `sudo docker compose logs -f agent`.

### Option 2: the agent on your own computer (free, while your computer is on)

`agent/linkedin-agent.mjs` drives your own Chrome, from your own network, signed in by you. It asks
the server for one job at a time: send an invite, send a follow-up to someone who accepted, check who
accepted, check for replies, read the pages a play watches. The server decides pacing and caps.

```bash
cd agent && npm install
```

```bash
SIGNALPIPE_URL=https://your-host SIGNALPIPE_TOKEN=<API_TOKEN> node linkedin-agent.mjs run
```

A Chrome window opens on LinkedIn's sign-in page. Sign in once, including any verification code; the
session is saved in the agent's own Chrome profile (`~/.signalpipe/chrome-profile`) and reused from
then on. To let it sign in by itself when the session expires, also set `LINKEDIN_EMAIL` and
`LINKEDIN_PASSWORD` in that shell. They are typed into LinkedIn's sign-in page and go nowhere else:
not to the Signalpipe server, not into any file the agent writes.

It works while your computer is on and the agent is running; steps approved while it is off go out
when it next starts. Run only one agent per LinkedIn account: if the server agent is also running,
stop it first (`sudo docker compose stop agent`). Each step is handed to one agent at a time, so two
agents never send the same step, but two browsers on two networks acting as one person is a signal of
its own.

By default the agent starts your ordinary Chrome as a normal window and attaches to it, so the
browser is not running in the automation mode that pages can see. It works the page the way you
would: it moves the pointer to a button before pressing it, types at a human pace, glances down a
profile before acting, and leaves minutes between sends. What it will not do: if LinkedIn shows a
security check it stops, tells the app, and waits for you to sign in again; it never tries to get
past a check, and it never changes what the browser reports about your device, network or identity.
None of this is a guarantee. LinkedIn also judges accounts on behaviour (volume, pending invites,
acceptance rate), and this has not been run against LinkedIn itself.

Set `LINKEDIN_CHROME_PATH` if Chrome is not in its usual place. `LINKEDIN_MODE=launch` makes the
automation library start the browser instead; that mode is for the tests and is visibly automated.
`LINKEDIN_DEBUG=1` prints each step with timings.

The agent finds buttons by their English labels ("Connect", "Add a note", "Send", "Message"), all
defined in one table at the top of the file. It has been tested end to end against a stand-in site
(`tests/fake_linkedin.mjs`), **not against LinkedIn itself**, so expect to adjust that table on first
use. To see what it detects on a real profile without doing anything:

```bash
node linkedin-agent.mjs doctor https://www.linkedin.com/in/someone
```

Tunables on the server: `LINKEDIN_DAILY_CAP` (20), `LINKEDIN_WEEKLY_CAP` (80), `LINKEDIN_HOURS`
(`9-18`), `LINKEDIN_TZ`, `LINKEDIN_MIN_GAP_MIN` / `LINKEDIN_MAX_GAP_MIN` (8 and 20 minutes between
sends), `LINKEDIN_FOLLOWUP_DELAY_HOURS` (`1-6`).

### Option 3: Unipile (paid, always on)

Set `UNIPILE_DSN` and `UNIPILE_API_KEY`, then press **Connect LinkedIn** in Settings to sign in on
[Unipile](https://www.unipile.com)'s hosted page; your password never reaches this app. Unipile runs
the session for you, so it works with your computer off.

### Either way

This is against LinkedIn's terms for third-party tools and carries account risk. Defaults are 20
invites a day and 80 a week, sent one at a time during working hours with random spacing, and the
sender pauses for 20 hours the moment LinkedIn signals a limit.

## Use it from Claude

`mcp_server.py` exposes the pipeline as MCP tools (list and approve leads, run plays, edit and approve
messages, read the inbox, validate an email or phone, run an SEO audit):

```json
{"mcpServers": {"signalpipe": {"command": "python", "args": ["mcp_server.py"],
  "env": {"SIGNALPIPE_URL": "https://your-host", "SIGNALPIPE_TOKEN": "<API_TOKEN>"}}}}
```

## What has and has not been exercised

Run against live services during development: every collector except `github` and `linkedin`, domain
resolution, site enrichment, email MX validation, phone validation, the SEO crawl, password and token
auth, the MCP tools, and the UI in a headless browser. Sequences, SMTP sending (against a local SMTP
server), reply, bounce and unsubscribe handling, CSV import and the LinkedIn flow (against a fake of
Unipile's documented responses) are covered by `tests/`.

The local setup was run end to end on Linux from a clean copy: the launcher installed everything with
uv and started the app; the app refused look-alike hosts and unsigned requests and signed the browser
in with the launcher's key; Connect LinkedIn started the agent with the runtime bundled in the Python
Playwright package, which signed in to the stand-in site and sent invites and a follow-up; starting it a
second time opened the running copy; quitting closed the LinkedIn browser too. The Mac and Windows
launchers have not been run, for lack of a Mac or Windows machine.

The agents were tested with the real Anthropic SDK against a scripted stand-in for the Messages API
(tool calls, paused turns, refusals, budget stops), and with a stand-in `claude` binary that drives the
real MCP bridge. The subscription engine was also run against real Claude Code: a Copilot conversation
looked up leads through the bridge and remembered the earlier turn.

Written from the vendors' documentation but not yet run against a real account: the API-key agent runs
against the live API with web search, Unipile, Hunter, Twilio Lookup, GitHub stargazers, and IMAP polling.
The full compose stack (app, LinkedIn agent container and Caddy) was built and run locally against the
stand-in LinkedIn site: the server agent signed in with saved credentials, sent invites and a
follow-up, kept its session across a container restart, stopped on a security check, and carried on
after the check was cleared by hand through the dashboard's screen; the screen refuses anyone not
signed in. Certificate issuance and the Lightsail launch script have not been run on a real instance,
and the agent has not been run against LinkedIn itself.

## Sending responsibly

Cold email and cold calling are regulated. The app enforces the mechanical parts (suppression list,
unsubscribe handling, postal address, send caps, manual-dial flag on US mobiles, business-hours
windows) but you are responsible for having a lawful basis to contact people in their jurisdiction,
for honouring do-not-call lists, and for keeping volumes at a level a person would send by hand.
