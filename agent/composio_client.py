"""Thin REST client for Composio: execute tools and read the toolkit catalog.

We call the REST API directly (instead of the SDK) so the agent runs on
Python 3.9 with only httpx installed. Two Composio surfaces are used:
  * COMPOSIO_SEARCH toolkit  -> web search + URL fetch for the research agent
  * /toolkits catalog        -> independent cross-check of auth schemes
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import httpx

from common import DATA, log, read_json, write_json

BASES = [
    os.getenv("COMPOSIO_BASE_URL", "https://backend.composio.dev/api/v3.1"),
    "https://backend.composio.dev/api/v3",
]
USER_ID = os.getenv("COMPOSIO_USER_ID", "app-research-agent")


def _key() -> str:
    k = os.getenv("COMPOSIO_API_KEY", "")
    if not k:
        raise RuntimeError("COMPOSIO_API_KEY missing in .env")
    return k


def _request(method: str, path: str, **kw: Any) -> httpx.Response:
    headers = {"x-api-key": _key(), "Content-Type": "application/json"}
    last: Optional[httpx.Response] = None
    for base in BASES:
        r = httpx.request(method, base + path, headers=headers, timeout=90, **kw)
        if r.status_code == 404 and base != BASES[-1]:
            last = r
            continue
        return r
    assert last is not None
    return last


def execute(slug: str, arguments: Dict[str, Any]) -> Any:
    """Run a Composio tool (no connected account needed for COMPOSIO_SEARCH)."""
    body = {"user_id": USER_ID, "arguments": arguments, "version": "latest"}
    r = _request("POST", f"/tools/execute/{slug}", json=body)
    if r.status_code >= 400 and "version" in r.text.lower():
        body.pop("version")
        r = _request("POST", f"/tools/execute/{slug}", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Composio {slug} HTTP {r.status_code}: {r.text[:300]}")
    js = r.json()
    if isinstance(js, dict) and js.get("successful") is False:
        raise RuntimeError(f"Composio {slug} failed: {str(js.get('error'))[:300]}")
    return js.get("data", js) if isinstance(js, dict) else js


# ---------------------------------------------------------------- catalog
CATALOG_PATH = DATA / "composio_catalog.json"


def catalog(refresh: bool = False) -> List[Dict[str, Any]]:
    """Every toolkit in Composio's catalog with its auth schemes (cached)."""
    if CATALOG_PATH.exists() and not refresh:
        return read_json(CATALOG_PATH)
    items: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(50):
        params: Dict[str, Any] = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = _request("GET", "/toolkits", params=params)
        r.raise_for_status()
        js = r.json()
        for it in js.get("items", []):
            meta = it.get("meta") or {}
            items.append(
                {
                    "slug": it.get("slug"),
                    "name": it.get("name"),
                    "auth_schemes": it.get("auth_schemes") or [],
                    "composio_managed": it.get("composio_managed_auth_schemes") or [],
                    "no_auth": it.get("no_auth"),
                    "tools_count": meta.get("tools_count"),
                    "app_url": meta.get("app_url"),
                }
            )
        cursor = js.get("next_cursor")
        if not cursor:
            break
    write_json(CATALOG_PATH, items)
    log(f"catalog: {len(items)} Composio toolkits cached")
    return items


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Names in the brief that differ from likely toolkit slugs.
ALIASES = {
    "Lark (Larksuite)": ["lark", "larksuite", "feishu"],
    "Meta Ads": ["metaads", "facebookads", "facebook"],
    "LinkedIn Ads": ["linkedinads", "linkedin"],
    "Threads (Meta)": ["threads"],
    "Magento (Adobe Commerce)": ["magento", "adobecommerce"],
    "Salesforce Commerce Cloud": ["salesforcecommercecloud", "commercecloud"],
    "Amazon Selling Partner": ["amazonsellingpartner", "amazonsp", "spapi", "amazonseller"],
    "WhatsApp Business": ["whatsapp", "whatsappbusiness"],
    "Monday.com": ["monday", "mondaycom"],
    "Otter AI": ["otter", "otterai"],
    "Zoho CRM": ["zohocrm", "zoho"],
    "Zoho Cliq": ["zohocliq", "cliq"],
    "MongoDB Atlas": ["mongodbatlas", "mongodb"],
    "Google Ads": ["googleads"],
    "YouTube Transcript": ["youtubetranscript", "transcriptapi"],
    "Mermaid CLI": ["mermaid", "mermaidcli"],
    "Waterfall.io": ["waterfall", "waterfallio"],
    "Close": ["close", "closecrm"],
    "Copper": ["copper", "coppercrm"],
    "Twenty": ["twenty", "twentycrm"],
    "Plain": ["plain", "plaincom"],
    "Front": ["front", "frontapp"],
    "Help Scout": ["helpscout"],
    "Bright Data": ["brightdata"],
    "SE Ranking": ["seranking"],
    "systeme.io": ["systemeio", "systeme"],
}


def match_toolkit(name: str, cat: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    keys = ALIASES.get(name) or [_norm(name), _norm(re.sub(r"\(.*?\)", "", name))]
    by_slug = {_norm(t["slug"]): t for t in cat}
    by_name = {_norm(t["name"]): t for t in cat}
    for k in keys:
        if k in by_slug:
            return by_slug[k]
        if k in by_name:
            return by_name[k]
    return None


SCHEME_MAP = {
    "OAUTH2": "oauth2",
    "OAUTH1": "oauth2",  # treated as OAuth family for comparison
    "API_KEY": "api_key",
    "BEARER_TOKEN": "bearer_token",
    "BASIC": "basic",
    "BASIC_WITH_JWT": "basic",
    "NO_AUTH": "none",
}


def catalog_auth_set(tk: Dict[str, Any]) -> List[str]:
    out = set()
    for s in tk.get("auth_schemes") or []:
        out.add(SCHEME_MAP.get(str(s).upper(), "other"))
    if tk.get("no_auth"):
        out.add("none")
    return sorted(out)
