"""PASS 1 - single-pass research agent.

For each app:  search (Composio COMPOSIO_SEARCH_WEB) -> pick official pages ->
fetch (direct, or Composio FETCH_URL_CONTENT for JS pages) -> keep relevant
paragraphs -> one LLM extraction into the schema with url+quote evidence.

No checking happens here on purpose: this is the baseline that the
verification loops in verify.py must improve on.

    python agent/research.py            # all 100 apps
    python agent/research.py --ids 1,2  # subset
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import llm  # noqa: E402
import web  # noqa: E402
from common import RUNS, log, read_apps, read_json, write_json  # noqa: E402
from schema import EXTRACT_SYSTEM, extract_prompt, normalize  # noqa: E402

MAX_PAGES = 6


def pick_urls(app: Dict[str, str], results: List[Dict[str, str]], limit: int) -> List[str]:
    """Official-domain pages first, then reputable others; skip obvious noise."""
    official = web.root_domain(app["hint"]) if app["hint"] else ""
    name_tok = app["name"].split()[0].lower().strip("().")
    noise = ("youtube.com", "reddit.com", "medium.com", "linkedin.com/posts", "facebook.com/groups", "quora.com",
             "g2.com", "capterra.com", "trustradius.com", "x.com", "twitter.com")
    hint = web.hint_url(app["hint"])
    ranked: List[tuple] = []
    for r in results:
        u = r["url"]
        if any(n in u for n in noise):
            continue
        d = web.domain(u)
        score = 0
        if official and official in d:
            score += 10
        if name_tok and name_tok in d:
            score += 6
        if any(k in u.lower() for k in ("developer", "docs", "api", "auth", "pricing", "mcp")):
            score += 3
        if "github.com" in d:
            score += 2
        ranked.append((-score, u))
    ranked.sort()
    urls: List[str] = [hint] if hint else []
    seen = {hint.split("?")[0].rstrip("/")} if hint else set()
    for _, u in ranked:
        key = u.split("?")[0].split("#")[0].rstrip("/")  # e.g. ?api-version= / ?lang= variants of one page
        if key not in seen:
            seen.add(key)
            urls.append(u)
    return urls[:limit]


def gather(app: Dict[str, str], queries: List[str], limit: int = MAX_PAGES) -> List[Dict[str, Any]]:
    results: List[Dict[str, str]] = []
    for q in queries:
        results += web.search(q, n=8)
    pages = []
    for u in pick_urls(app, results, limit + 3):
        p = web.fetch(u)
        if len(p.get("text", "")) < 200:
            continue
        pages.append({"url": u, "via": p["via"], "chars": len(p["text"]), "excerpt": web.excerpt(p["text"], 6000)})
        if len(pages) >= limit:
            break
    return pages


def research_app(app: Dict[str, str], force: bool = False) -> Dict[str, Any]:
    out_path = RUNS / "pass1" / f"{int(app['id']):03d}.json"
    if out_path.exists() and not force:
        return read_json(out_path)
    t0 = time.time()
    name = app["name"]
    queries = [f"{name} API authentication developer docs", f"{name} API access pricing plan developer", f"{name} MCP server"]
    pages = gather(app, queries)
    rec: Dict[str, Any]
    try:
        raw = llm.call("extractor", extract_prompt(app, pages), EXTRACT_SYSTEM, cache_key=f"p1|{app['id']}|{len(pages)}")
        rec = normalize(dict(raw))
        rec["status"] = "ok"
    except Exception as e:  # noqa: BLE001
        rec = normalize({})
        rec["status"] = f"error: {str(e)[:200]}"
    rec.update({
        "id": int(app["id"]), "name": name, "category": app["category"], "hint": app["hint"],
        "sources": [{"url": p["url"], "via": p["via"], "chars": p["chars"]} for p in pages],
        "queries": queries, "seconds": round(time.time() - t0, 1),
    })
    write_json(out_path, rec)
    log(f"[pass1] {app['id']:>3} {name:<28} {rec['auth_primary']:<13} {rec['access_tier']:<18} {rec['verdict']:<20} "
        f"pages={len(pages)} {rec['status'][:40]}")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="comma-separated app ids (default all)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    apps = read_apps()
    if a.ids:
        want = {x.strip() for x in a.ids.split(",")}
        apps = [x for x in apps if x["id"] in want]
    log(f"pass1: researching {len(apps)} apps with {a.workers} workers")
    with ThreadPoolExecutor(a.workers) as ex:
        recs = list(ex.map(lambda x: research_app(x, a.force), apps))
    all_recs = sorted([read_json(p) for p in (RUNS / "pass1").glob("*.json")], key=lambda r: r["id"])
    write_json(RUNS / "pass1.json", all_recs)
    errs = [r for r in recs if r.get("status") != "ok"]
    log(f"pass1 done: {len(recs)} apps, {len(errs)} errors. web={web.STATS} llm={llm.USAGE}")


if __name__ == "__main__":
    main()
