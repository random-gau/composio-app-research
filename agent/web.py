"""Web search + page fetching, with caching and fallbacks.

search(): Composio's COMPOSIO_SEARCH_WEB tool first, DuckDuckGo (ddgs) fallback.
fetch():  direct HTTP + BeautifulSoup text extraction; for JS-heavy pages that
          return little text, fall back to Composio's FETCH_URL_CONTENT tool.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

import composio_client
from common import cache_get, cache_put, log

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

STATS = {"search_composio": 0, "search_ddg": 0, "fetch_direct": 0, "fetch_composio": 0, "fetch_fail": 0}


def _walk_links(obj: Any, out: List[Dict[str, str]]) -> None:
    if isinstance(obj, dict):
        url = obj.get("link") or obj.get("url") or obj.get("href")
        if isinstance(url, str) and url.startswith("http"):
            snip = obj.get("snippet") or obj.get("description") or obj.get("content") or ""
            out.append({"url": url, "title": str(obj.get("title", ""))[:200], "snippet": str(snip)[:400]})
        for v in obj.values():
            _walk_links(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_links(v, out)


def _dedupe(results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen, out = set(), []
    for r in results:
        u = r["url"].split("#")[0].rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        r["url"] = u
        out.append(r)
    return out


def search(query: str, n: int = 6) -> List[Dict[str, str]]:
    cached = cache_get("search", query)
    if cached is not None:
        return cached[:n]
    results: List[Dict[str, str]] = []
    try:
        data = composio_client.execute("COMPOSIO_SEARCH_WEB", {"query": query})
        _walk_links(data, results)
        results = [r for r in _dedupe(results) if "composio.dev" not in r["url"]]
        if results:
            STATS["search_composio"] += 1
    except Exception as e:  # noqa: BLE001
        log(f"  composio search failed ({str(e)[:120]}), falling back to ddg")
    if not results:
        try:
            try:
                from ddgs import DDGS  # type: ignore
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore
            with DDGS() as d:
                for r in d.text(query, max_results=10):
                    results.append({"url": r.get("href", ""), "title": r.get("title", ""), "snippet": r.get("body", "")})
            results = _dedupe([r for r in results if r["url"].startswith("http")])
            STATS["search_ddg"] += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ddg search failed: {str(e)[:120]}")
    if not results:
        results = _ddg_html(query)
    if results:
        cache_put("search", query, results)
    return results[:n]


def _ddg_html(query: str) -> List[Dict[str, str]]:
    """Last-resort search: DuckDuckGo's HTML endpoint via plain httpx."""
    from urllib.parse import parse_qs, unquote
    try:
        r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": UA}, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(href.split("?", 1)[1]).get("uddg", [""])[0])
            if href.startswith("http") and "duckduckgo.com" not in href:
                out.append({"url": href, "title": a.get_text(" ", strip=True), "snippet": ""})
        if out:
            STATS["search_ddg"] += 1
        return _dedupe(out)
    except Exception as e:  # noqa: BLE001
        log(f"  ddg html search failed: {str(e)[:120]}")
        return []


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return (title + "\n" + text).strip()


def _longest_string(obj: Any) -> str:
    best = ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        it = obj.values()
    elif isinstance(obj, list):
        it = obj
    else:
        return ""
    for v in it:
        s = _longest_string(v)
        if len(s) > len(best):
            best = s
    return best


def fetch(url: str) -> Dict[str, Any]:
    """Return {'url','final_url','status','text','via'}; cached on disk."""
    cached = cache_get("pages", url)
    if cached is not None:
        return cached
    page: Dict[str, Any] = {"url": url, "final_url": url, "status": 0, "text": "", "via": "none"}
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=25, follow_redirects=True)
        page["status"] = r.status_code
        page["final_url"] = str(r.url)
        ctype = r.headers.get("content-type", "")
        if r.status_code < 400:
            if "html" in ctype or r.text.lstrip().startswith("<"):
                page["text"] = _html_to_text(r.text)
            elif "json" in ctype or "text" in ctype or "markdown" in ctype:
                page["text"] = r.text
            page["via"] = "direct"
    except Exception as e:  # noqa: BLE001
        page["error"] = str(e)[:200]
    if len(page["text"]) < 800:
        # JS-rendered docs (SPAs) or bot-blocked pages: ask Composio to fetch.
        for args in ({"url": url}, {"urls": [url]}):
            try:
                data = composio_client.execute("COMPOSIO_SEARCH_FETCH_URL_CONTENT", args)
                txt = _longest_string(data)
                if len(txt) > len(page["text"]):
                    page["text"], page["via"] = txt, "composio_fetch"
                    STATS["fetch_composio"] += 1
                break
            except Exception as e:  # noqa: BLE001
                page["composio_error"] = str(e)[:200]
    if page["via"] == "direct":
        STATS["fetch_direct"] += 1
    if not page["text"]:
        STATS["fetch_fail"] += 1
    page["text"] = page["text"][:200_000]
    if page["text"]:  # don't cache failures; a later run may succeed
        cache_put("pages", url, page)
    return page


def domain(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def root_domain(url_or_hint: str) -> str:
    h = url_or_hint.strip().split()[0]
    if not h.startswith("http"):
        h = "https://" + h
    parts = domain(h).split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain(h)


def hint_url(hint: str) -> Optional[str]:
    h = re.sub(r"\(.*?\)", "", hint).strip().split()[0] if hint.strip() else ""
    if not h or "." not in h:
        return None
    return h if h.startswith("http") else "https://" + h


KEYWORDS = [
    "oauth", "api key", "api_key", "apikey", "token", "bearer", "basic auth", "authenticat", "authoriz",
    "client id", "client secret", "scope", "pricing", "plan", "free", "trial", "enterprise", "contact sales",
    "partner", "approval", "review", "apply", "access", "rest", "graphql", "endpoint", "mcp",
    "model context protocol", "sandbox", "developer", "rate limit", "sdk",
]


def excerpt(text: str, budget: int = 6000) -> str:
    """Keep the paragraphs most relevant to auth/access/API/MCP, in original order."""
    if len(text) <= budget:
        return text
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    scored = []
    for i, p in enumerate(paras):
        low = p.lower()
        score = sum(low.count(k) for k in KEYWORDS)
        scored.append((score, i, p))
    keep, used = set(), 0
    head = paras[:8]  # title + intro usually says what the product is
    for p in head:
        used += len(p) + 1
    for score, i, p in sorted(scored, key=lambda x: (-x[0], x[1])):
        if score == 0 or used > budget:
            break
        keep.add(i)
        used += len(p) + 1
    out = [p for i, p in enumerate(paras) if i < 8 or i in keep]
    return "\n".join(out)[: budget + 500]
