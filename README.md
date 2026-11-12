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
   - Email: syntax, disposable and role detection, MX lookup, suppression list. Mailbox-level
     verification needs `HUNTER_API_KEY`, or `SMTP_PROBE=1` on a host with outbound port 25. Without
     either, a guessed address is labelled "risky, pattern guess" and never shown as verified.
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
   - In "Review each step" mode nothing sends until you approve it. In "Send automatically" mode only
     the first email to a verified address goes out unreviewed.
8. **SEO.** Crawls your site (robots, sitemap, titles, descriptions, headings, canonicals, thin
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

LinkedIn has no public API for reading post engagers or sending invitations. Hosted tools get this by
running a session on your account through a provider; this app does the same through
[Unipile](https://www.unipile.com) when `UNIPILE_DSN` and `UNIPILE_API_KEY` are set. Press **Connect
LinkedIn** in Settings to sign in on Unipile's hosted page; your password never reaches this app.

This is against LinkedIn's terms for third-party tools and carries account risk. Defaults are 20
invites a day and 80 a week, sent one at a time during working hours with random spacing, and the
sender pauses for 20 hours the moment LinkedIn signals a limit. Without Unipile the app never touches
LinkedIn; you send each step yourself from the queue.

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
key was available), Unipile, Hunter, Twilio Lookup, GitHub stargazers, IMAP polling, and the Docker
image build.

## Sending responsibly

Cold email and cold calling are regulated. The app enforces the mechanical parts (suppression list,
unsubscribe handling, postal address, send caps, manual-dial flag on US mobiles, business-hours
windows) but you are responsible for having a lawful basis to contact people in their jurisdiction,
for honouring do-not-call lists, and for keeping volumes at a level a person would send by hand.
