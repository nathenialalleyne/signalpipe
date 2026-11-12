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

## Run it

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

1. In the Lightsail console create an instance: Linux, **Ubuntu 24.04**, the 1 GB plan (the 512 MB
   plan works but builds slowly). Under **Add launch script**, paste the contents of
   [`deploy/lightsail-launch.sh`](deploy/lightsail-launch.sh). To use your own domain, fill in
   `DOMAIN=""` at the top of the script first.
2. On the instance's **Networking** tab, attach a static IP and add a firewall rule for **HTTPS (443)**.
   Port 80 must stay open too; it is used to issue the certificate.
3. After about five minutes, open the browser SSH terminal and read the address and generated password:

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
operate your account as you. There are two ways to do the same here; without either, LinkedIn steps
wait in a queue for you to send by hand.

### Option 1: the agent on your own computer (free)

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
when it next starts.

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

### Option 2: Unipile (paid, always on)

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

Written from the vendors' documentation but not yet run against a real account: Claude calls (no API
key was available), Unipile, Hunter, Twilio Lookup, GitHub stargazers, and IMAP polling.
The Docker image and the compose stack (app behind Caddy) were built and started locally; certificate
issuance and the Lightsail launch script have not been run on a real instance.

## Sending responsibly

Cold email and cold calling are regulated. The app enforces the mechanical parts (suppression list,
unsubscribe handling, postal address, send caps, manual-dial flag on US mobiles, business-hours
windows) but you are responsible for having a lawful basis to contact people in their jurisdiction,
for honouring do-not-call lists, and for keeping volumes at a level a person would send by hand.
