"""PASS 2 - verification loops on top of pass 1.

Per app, up to MAX_ROUNDS rounds of:
  L1 evidence check   - re-fetch each cited URL, confirm the quote really is on that page
                        (catches invented quotes / URLs, the classic LLM failure)
  L2 consistency rules- verdict vs access tier vs blocker vs API type must agree
  L3 Composio catalog - compare auth methods with Composio's own toolkit catalog
  L4 blind verifier   - a different model family (Groq/Llama) answers from the same sources
                        without seeing pass 1; disagreements are flagged
  L5 MCP probe        - targeted search for an official MCP server when pass 1 found none
Flagged fields trigger TARGETED re-research (new searches for that field) and an
adjudication call that must cite a verbatim quote. Whatever is still unresolved after
the last round is marked needs_human and goes to data/human_review_queue.csv.

    python agent/verify.py [--ids 1,2] [--workers 3]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import composio_client  # noqa: E402
import llm  # noqa: E402
import web  # noqa: E402
from common import DATA, RUNS, log, read_apps, read_json, write_json  # noqa: E402
from research import gather  # noqa: E402
from schema import (ADJUDICATE_SYSTEM, AUTH, FIELDS, VERIFY_SYSTEM, adjudicate_prompt,  # noqa: E402
                    normalize, verify_prompt)

MAX_ROUNDS = 2
_CATALOG: Optional[List[Dict[str, Any]]] = None


# ------------------------------------------------------------------ L1 quote check
def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def quote_on_page(quote: str, url: str) -> Tuple[str, float]:
    """Return (status, score). status: ok | partial | not_found | fetch_failed."""
    page = web.fetch(url)
    text = page.get("text", "")
    if len(text) < 100:
        return "fetch_failed", 0.0
    q, t = _norm_text(quote), _norm_text(text)
    if not q:
        return "not_found", 0.0
    if q in t:
        return "ok", 1.0
    words = q.split()
    if len(words) < 4:
        return "not_found", 0.0
    grams = {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}
    hit = sum(1 for g in grams if g in t) / max(len(grams), 1)
    if hit >= 0.8:
        return "ok", hit
    if hit >= 0.5:
        return "partial", hit
    return "not_found", hit


def check_evidence(rec: Dict[str, Any], fields: List[str]) -> List[Dict[str, Any]]:
    issues = []
    src_urls = {s["url"] for s in rec.get("sources", [])} | set(rec.get("_extra_urls", []))
    for f in fields:
        ev = (rec.get("evidence") or {}).get(f) or {}
        url, quote = ev.get("url"), ev.get("quote")
        if f == "mcp" and rec.get("mcp") == "none_found" and not url:
            continue  # absence can't be quoted; handled by the MCP probe
        if f == "verdict" and not url:
            continue  # verdict is derived from access+api; checked by L2 rules and L4 verifier
        if not url or not quote:
            issues.append({"loop": "L1_evidence", "field": f, "problem": "no evidence cited"})
            continue
        status, score = quote_on_page(quote, url)
        ev["check"] = status
        if status == "ok":
            if url not in src_urls:
                ev["check"] = "ok_url_outside_sources"
            continue
        problem = {"not_found": "quote not found on cited page", "partial": "quote only partially matches page",
                   "fetch_failed": "cited page could not be fetched"}[status]
        if url not in src_urls:
            problem += " (URL was not among the sources given)"
        issues.append({"loop": "L1_evidence", "field": f, "problem": problem, "url": url, "match": round(score, 2)})
    return issues


# ------------------------------------------------------------------ L2 rules
def check_rules(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    iss = []
    a, v, b, api = rec["access_tier"], rec["verdict"], rec["blocker"], rec["api_types"]

    def add(field: str, msg: str) -> None:
        iss.append({"loop": "L2_rules", "field": field, "problem": msg})

    if v == "build_now" and a in ("paid_plan", "self_serve_trial", "approval_or_review", "partner_or_sales"):
        add("verdict", f"verdict build_now contradicts access_tier {a}")
    if v == "build_now" and api == ["none"]:
        add("verdict", "verdict build_now but no public API type")
    if v == "blocked" and a == "self_serve_free" and api != ["none"]:
        add("verdict", "verdict blocked but access is self-serve free with a public API")
    if a == "partner_or_sales" and v == "build_now":
        add("access", "partner/sales gate but marked buildable now")
    if a == "open_source_local" and v == "build_now":
        add("verdict", "open-source local tool has no hosted API or credentials; build_now needs justification")
    if rec["mcp"] == "official" and not rec.get("mcp_url"):
        add("mcp", "official MCP claimed without a URL")
    if a == "unknown":
        add("access", "access tier unknown")
    if rec["auth_methods"] == ["other"]:
        add("auth", "auth method not identified")
    return iss


def autofix(rec: Dict[str, Any]) -> List[str]:
    """Deterministic fixes that need no judgement."""
    fixes = []
    if rec["verdict"] == "build_now" and rec["blocker"] != "none":
        rec["blocker"] = "none"
        fixes.append("blocker reset to none for build_now")
    if rec["verdict"] != "build_now" and rec["blocker"] == "none":
        rec["blocker"] = {"paid_plan": "paid_plan", "self_serve_trial": "paid_plan", "approval_or_review": "app_review",
                          "partner_or_sales": "partner_approval", "open_source_local": "local_tool_only"}.get(
            rec["access_tier"], "docs_unclear")
        fixes.append(f"blocker set to {rec['blocker']} from access tier")
    return fixes


# ------------------------------------------------------------------ L3 catalog
def check_catalog(rec: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    global _CATALOG
    if _CATALOG is None:
        try:
            _CATALOG = composio_client.catalog()
        except Exception as e:  # noqa: BLE001
            log(f"catalog unavailable: {e}")
            _CATALOG = []
    tk = composio_client.match_toolkit(rec["name"], _CATALOG) if _CATALOG else None
    if not tk:
        return [], {"in_catalog": False}
    cat_auth = composio_client.catalog_auth_set(tk)
    info = {"in_catalog": True, "slug": tk["slug"], "auth": cat_auth, "tools_count": tk.get("tools_count")}
    agent = set(rec["auth_methods"])
    # bearer_token vs api_key naming differs between vendors; treat as one family for this check
    fam = lambda s: {("api_key" if x == "bearer_token" else x) for x in s}  # noqa: E731
    cat_set = fam(set(cat_auth)) - {"other"}
    if cat_set and not (fam(agent) & cat_set):
        return [{"loop": "L3_catalog", "field": "auth", "problem": f"agent auth {sorted(agent)} shares nothing with "
                 f"Composio catalog {cat_auth} ({tk['slug']})"}], info
    missing = cat_set - fam(agent)
    if missing:
        return [{"loop": "L3_catalog", "field": "auth", "problem": f"Composio catalog also lists {sorted(missing)} "
                 f"for {tk['slug']}; agent missed it"}], info
    return [], info


# ------------------------------------------------------------------ L4 blind verifier
def run_verifier(app: Dict[str, str], pages: List[Dict[str, Any]], tag: str) -> Dict[str, Any]:
    # Short, keyword-focused excerpts: the verifier runs ~150 times on a free token budget.
    small = [{"url": p["url"], "excerpt": web.excerpt(web.fetch(p["url"]).get("text", "") or p["excerpt"], 1600)}
             for p in pages[:4]]
    try:
        raw = llm.call("verifier", verify_prompt(app, small), VERIFY_SYSTEM, cache_key=f"v|{tag}|{app['id']}|{len(small)}")
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    v = dict(raw)
    tmp = normalize({"auth_methods": v.get("auth_methods"), "auth_primary": v.get("auth_primary"),
                     "access_tier": v.get("access_tier"), "api_types": v.get("api_types"), "mcp": v.get("mcp"),
                     "verdict": v.get("verdict")})
    raw_mcp = str(v.get("mcp", "")).lower()
    raw_verdict = str(v.get("verdict", "")).lower()
    return {"auth_methods": tmp["auth_methods"], "auth_primary": tmp["auth_primary"], "access_tier": tmp["access_tier"],
            "api_types": tmp["api_types"], "mcp": tmp["mcp"] if raw_mcp in ("official", "community_only", "none_found") else "unknown",
            "verdict": tmp["verdict"] if raw_verdict in ("build_now", "build_with_friction", "blocked") else "unknown",
            "reasons": v.get("reasons", {}), "_model": v.get("_model")}


def compare(rec: Dict[str, Any], ver: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not ver or ver.get("error"):
        return []
    iss = []

    def add(field: str, msg: str) -> None:
        iss.append({"loop": "L4_verifier", "field": field, "problem": msg})

    if ver["auth_methods"] != ["other"] and not (set(ver["auth_methods"]) & set(rec["auth_methods"])):
        add("auth", f"verifier says {ver['auth_methods']} vs agent {rec['auth_methods']}")
    if ver["access_tier"] != "unknown" and ver["access_tier"] != rec["access_tier"]:
        add("access", f"verifier says {ver['access_tier']} vs agent {rec['access_tier']}")
    if ver["mcp"] != "unknown" and ver["mcp"] != rec["mcp"]:
        add("mcp", f"verifier says {ver['mcp']} vs agent {rec['mcp']}")
    if ver["verdict"] != "unknown" and ver["verdict"] != rec["verdict"]:
        add("verdict", f"verifier says {ver['verdict']} vs agent {rec['verdict']}")
    if ver["api_types"] != ["none"] and not (set(ver["api_types"]) & set(rec["api_types"])):
        add("api", f"verifier says {ver['api_types']} vs agent {rec['api_types']}")
    return iss


# ------------------------------------------------------------------ L5 MCP probe
def mcp_probe(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if rec["mcp"] == "official":
        return []
    name = re.sub(r"\(.*?\)", "", rec["name"]).strip()
    official = web.root_domain(rec["hint"]) if rec["hint"] else ""
    tok = re.sub(r"[^a-z0-9]", "", name.lower())
    hits = []
    for r in web.search(f"{name} official MCP server", n=8):
        u, d = r["url"].lower(), web.domain(r["url"])
        vendor = (official and official in d) or (tok and tok in d.replace(".", "").replace("-", ""))
        vendor_repo = "github.com/" in u and tok and tok in u.split("github.com/")[1].split("/")[0].replace("-", "")
        if ("mcp" in u or "model context protocol" in (r.get("title", "") + r.get("snippet", "")).lower()) and (vendor or vendor_repo):
            hits.append(r["url"])
    if hits:
        return [{"loop": "L5_mcp_probe", "field": "mcp", "problem": f"possible official MCP found: {hits[:2]}", "urls": hits[:3]}]
    return []


# ------------------------------------------------------------------ re-research
FIELD_QUERIES = {
    "auth": ["{n} API authentication OAuth API key", "{n} API reference authentication"],
    "access": ["{n} API access requirements plan", "{n} developer account API key free OR partner OR enterprise"],
    "api": ["{n} API reference endpoints"],
    "mcp": ["{n} MCP server model context protocol"],
    "verdict": ["{n} API access requirements plan"],
}


def reresearch(app: Dict[str, str], rec: Dict[str, Any], issues: List[Dict[str, Any]], rnd: int) -> List[Dict[str, Any]]:
    name = re.sub(r"\(.*?\)", "", app["name"]).strip()
    queries: List[str] = []
    for f in sorted({i["field"] for i in issues}):
        for q in FIELD_QUERIES.get(f, []):
            q = q.format(n=name)
            if q not in queries:
                queries.append(q)
    new = gather(app, queries, limit=5)
    extra = [u for i in issues for u in i.get("urls", [])]
    for u in extra:
        p = web.fetch(u)
        if len(p.get("text", "")) > 200:
            new.append({"url": u, "via": p["via"], "chars": len(p["text"]), "excerpt": web.excerpt(p["text"], 5000)})
    seen, out = set(), []
    for p in new:
        if p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    rec.setdefault("_extra_urls", []).extend(p["url"] for p in out)
    rec.setdefault("verification_queries", []).extend(queries)
    return out


ADJ_KEYS = ["auth_methods", "auth_primary", "access_tier", "access_notes", "api_types", "api_breadth", "api_notes",
            "mcp", "mcp_url", "verdict", "blocker", "blocker_notes"]
SCORED = {"auth": ["auth_primary", "auth_methods"], "access": ["access_tier"], "api": ["api_types"], "mcp": ["mcp"],
          "verdict": ["verdict"]}


def snapshot(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (list(rec[k]) if isinstance(rec.get(k), list) else rec.get(k)) for k in ADJ_KEYS}


# ------------------------------------------------------------------ driver
def verify_app(app: Dict[str, str], force: bool = False) -> Dict[str, Any]:
    out_path = RUNS / "pass2" / f"{int(app['id']):03d}.json"
    if out_path.exists() and not force:
        return read_json(out_path)
    p1 = read_json(RUNS / "pass1" / f"{int(app['id']):03d}.json")
    rec = normalize(dict(p1))
    before = snapshot(rec)
    t0 = time.time()
    pages = []
    for s in rec.get("sources", []):
        pg = web.fetch(s["url"])
        pages.append({"url": s["url"], "via": pg["via"], "chars": len(pg.get("text", "")), "excerpt": web.excerpt(pg.get("text", ""), 6000)})

    rounds = []
    fixes = autofix(rec)
    ver = run_verifier(app, pages, "r0")
    cat_issues, cat_info = check_catalog(rec)
    issues = check_evidence(rec, FIELDS) + check_rules(rec) + cat_issues + compare(rec, ver) + mcp_probe(rec)
    initial_issues = list(issues)
    if ver.get("error"):
        log(f"  WARNING {app['name']}: blind verifier unavailable ({ver['error'][:100]})")
    rounds.append({"round": 0, "issues": issues, "verifier": ver, "autofix": fixes})
    human: Dict[str, bool] = {}

    for rnd in range(1, MAX_ROUNDS + 1):
        if not issues:
            break
        new_pages = reresearch(app, rec, issues, rnd)
        pool, seen_u = [], set()
        for p in new_pages + pages:
            if p["url"] not in seen_u:
                seen_u.add(p["url"])
                pool.append(p)
        pool_small = [{"url": p["url"], "excerpt": p["excerpt"][:4000]} for p in pool[:9]]
        try:
            adj = llm.call("extractor", adjudicate_prompt(app, rec, issues, ver, pool_small), ADJUDICATE_SYSTEM,
                           cache_key=f"adj|{app['id']}|{rnd}|{len(pool_small)}")
        except Exception as e:  # noqa: BLE001
            rounds.append({"round": rnd, "error": str(e)[:200]})
            break
        for k in ADJ_KEYS:
            if k in adj and adj[k] not in (None, "", []):
                rec[k] = adj[k]
        for f, ev in (adj.get("evidence") or {}).items():
            if f in FIELDS and isinstance(ev, dict) and ev.get("url"):
                rec["evidence"][f] = ev
        human = {f: bool(v) for f, v in (adj.get("needs_human") or {}).items() if f in FIELDS}
        rec = normalize(rec)
        fixes = autofix(rec)
        pages = pool
        flagged = sorted({i["field"] for i in issues})
        ver = run_verifier(app, pool, f"r{rnd}")
        cat_issues, cat_info = check_catalog(rec)
        issues = (check_evidence(rec, flagged) + check_rules(rec) + cat_issues +
                  [i for i in compare(rec, ver) if i["field"] in flagged])
        rounds.append({"round": rnd, "queries": rec.get("verification_queries", [])[-6:],
                       "new_sources": [p["url"] for p in new_pages], "change_log": adj.get("change_log"),
                       "issues": issues, "verifier": ver, "autofix": fixes})

    after = snapshot(rec)
    changed = sorted({f for f, keys in SCORED.items() if any(before[k] != after[k] for k in keys)})
    open_fields = sorted({i["field"] for i in issues} | {f for f, v in human.items() if v})
    initial_fields = sorted({i["field"] for i in initial_issues})
    status = {}
    for f in FIELDS:
        if f in open_fields:
            status[f] = "needs_human"
        elif f in changed:
            status[f] = "corrected"
        elif f in initial_fields:
            status[f] = "confirmed_after_recheck"
        else:
            status[f] = "verified"
    rec["verification"] = {
        "initial_issues": initial_issues, "final_issues": issues, "rounds": rounds, "catalog": cat_info,
        "changed_fields": changed, "field_status": status, "needs_human": open_fields, "before": before,
        "seconds": round(time.time() - t0, 1),
    }
    rec["sources"] = rec.get("sources", []) + [{"url": u, "via": "verification"} for u in rec.pop("_extra_urls", [])
                                                if u not in {s["url"] for s in rec.get("sources", [])}]
    write_json(out_path, rec)
    log(f"[pass2] {app['id']:>3} {app['name']:<28} issues {len(initial_issues)}->{len(issues)} "
        f"changed={changed or '-'} human={open_fields or '-'}")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    apps = read_apps()
    if a.ids:
        want = {x.strip() for x in a.ids.split(",")}
        apps = [x for x in apps if x["id"] in want]
    apps = [x for x in apps if (RUNS / "pass1" / f"{int(x['id']):03d}.json").exists()]
    log(f"pass2: verifying {len(apps)} apps")
    try:
        composio_client.catalog()
    except Exception as e:  # noqa: BLE001
        log(f"catalog fetch failed: {e}")
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(lambda x: verify_app(x, a.force), apps))
    recs = sorted([read_json(p) for p in (RUNS / "pass2").glob("*.json")], key=lambda r: r["id"])
    write_json(RUNS / "pass2.json", recs)
    with open(DATA / "human_review_queue.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "field", "agent_value", "open_issues", "evidence_url"])
        for r in recs:
            for fld in r["verification"]["needs_human"]:
                val = {"auth": r["auth_methods"], "access": r["access_tier"], "api": r["api_types"], "mcp": r["mcp"],
                       "verdict": r["verdict"]}[fld]
                probs = "; ".join(i["problem"] for i in r["verification"]["final_issues"] if i["field"] == fld)
                w.writerow([r["id"], r["name"], fld, val, probs, (r["evidence"].get(fld) or {}).get("url")])
    n_h = sum(1 for r in recs if r["verification"]["needs_human"])
    log(f"pass2 done. {n_h} apps have fields needing a human -> data/human_review_queue.csv. llm={llm.USAGE} web={web.STATS}")


if __name__ == "__main__":
    main()
