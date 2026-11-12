"""Recent stargazers of the repos you list (a competitor's open-source repo, or a category-defining one).

Uses the public GitHub REST API. Unauthenticated calls are limited to 60 per hour, so set GITHUB_TOKEN
for anything beyond a small repo list. Only public profile fields are read.
"""
import datetime as dt

from .. import config, web
from . import Signal, cutoff, terms

API = "https://api.github.com"


def _headers(star: bool = False) -> dict:
    h = {"Accept": "application/vnd.github.star+json" if star else "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


async def collect(cfg: dict) -> list[Signal]:
    since = cutoff(cfg)
    budget = 150 if config.GITHUB_TOKEN else 25  # profile lookups per run
    out: list[Signal] = []
    for repo in terms(cfg, "github_repos")[:10]:
        try:
            info = await web.get_json(f"{API}/repos/{repo}", headers=_headers())
            total = int(info.get("stargazers_count") or 0)
            last = max(1, -(-total // 100))
            # Stargazers are listed oldest first, so the newest are on the last page.
            stars = await web.get_json(f"{API}/repos/{repo}/stargazers",
                                       params={"per_page": 100, "page": last}, headers=_headers(star=True))
        except Exception:
            continue
        for st in reversed(stars):
            try:
                ts = dt.datetime.fromisoformat(st["starred_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts < since or budget <= 0:
                break
            login = st["user"]["login"]
            try:
                u = await web.get_json(f"{API}/users/{login}", headers=_headers())
            except Exception:
                break  # most likely rate limited
            budget -= 1
            company = (u.get("company") or "").lstrip("@").strip()
            blog = u.get("blog") or ""
            if not (company or u.get("email") or blog):
                continue  # nothing to act on
            out.append(Signal(
                source="github", type="repo_star", title=f"{u.get('name') or login} starred {repo}",
                text=(u.get("bio") or "")[:500], url=f"https://github.com/{login}", occurred_at=ts,
                company_name=company, company_domain=web.host_of(blog) if blog and not company else "",
                person_name=u.get("name") or "", person_handle=f"gh:{login}", email=u.get("email") or "",
                extra={"repo": repo, "location": u.get("location"), "followers": u.get("followers"), "blog": blog},
            ))
    return out
