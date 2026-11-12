"""Shared HTTP client and HTML helpers. Polite by default: one UA, timeouts, robots.txt for site crawls."""
import asyncio
import re
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

_client: httpx.AsyncClient | None = None
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_host_locks: dict[str, asyncio.Semaphore] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en"},
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
        )
    return _client


async def close() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_json(url: str, **kw):
    r = await client().get(url, **kw)
    r.raise_for_status()
    return r.json()


async def get_text(url: str, **kw) -> str:
    r = await client().get(url, **kw)
    r.raise_for_status()
    return r.text


def host_of(url: str) -> str:
    h = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


async def _robots_for(base: str):
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = await client().get(base + "/robots.txt", timeout=8)
            if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                rp.parse(r.text.splitlines())
                _robots[base] = rp
            else:
                _robots[base] = None
        except Exception:
            _robots[base] = None
    return _robots[base]


async def fetch_page(url: str, respect_robots: bool = True, timeout: float = 15.0) -> httpx.Response | None:
    """GET one page of a third-party site. Returns None when blocked by robots.txt or on any failure."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    base = f"{p.scheme}://{p.netloc}"
    if respect_robots:
        rp = await _robots_for(base)
        if rp is not None and not rp.can_fetch(config.USER_AGENT, url):
            return None
    sem = _host_locks.setdefault(p.netloc, asyncio.Semaphore(2))
    async with sem:
        try:
            r = await client().get(url, timeout=timeout)
        except Exception:
            return None
    if "html" not in r.headers.get("content-type", "html"):
        return None
    return r


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def visible_text(soup: BeautifulSoup, limit: int = 0) -> str:
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:limit] if limit else text


def strip_html(html: str, limit: int = 0) -> str:
    return visible_text(soup_of(html or ""), limit)


def links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:")):
            continue
        out.append((urljoin(base_url, href), a.get_text(" ", strip=True)))
    return out
