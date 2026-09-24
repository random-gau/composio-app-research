"""Build the case-study page (docs/index.html) and machine-readable results (docs/results.json).

Everything on the page is computed from data/runs/pass2.json, data/runs/pass1.json,
data/accuracy.json and data/composio_catalog.json, so the page can't drift from the data.
Hand-written headline wording (after reading the results) lives in data/insights.json.

    python agent/build_site.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from common import DATA, ROOT, RUNS, read_json, write_json  # noqa: E402

SITE = ROOT / "docs"  # GitHub Pages serves /docs on main
TEMPLATE = Path(__file__).resolve().parent / "site_template.html"


def slim(r: Dict[str, Any]) -> Dict[str, Any]:
    v = r.get("verification") or {}
    return {
        "id": r["id"], "name": r["name"], "category": r["category"], "hint": r["hint"],
        "one_liner": r.get("one_liner", ""),
        "auth_methods": r["auth_methods"], "auth_primary": r["auth_primary"], "auth_notes": r.get("auth_notes", ""),
        "access_tier": r["access_tier"], "access_notes": r.get("access_notes", ""),
        "api_types": r["api_types"], "api_breadth": r.get("api_breadth"), "api_notes": r.get("api_notes", ""),
        "mcp": r["mcp"], "mcp_url": r.get("mcp_url"),
        "verdict": r["verdict"], "blocker": r["blocker"], "blocker_notes": r.get("blocker_notes", ""),
        "evidence": {k: {"url": (e or {}).get("url"), "quote": (e or {}).get("quote"), "check": (e or {}).get("check")}
                     for k, e in (r.get("evidence") or {}).items()},
        "composio_catalog": v.get("catalog", {}),
        "field_status": v.get("field_status", {}),
        "changed_fields": v.get("changed_fields", []),
        "needs_human": v.get("needs_human", []),
        "issues_initial": [{"loop": i["loop"], "field": i["field"], "problem": i["problem"]} for i in v.get("initial_issues", [])],
        "issues_final": [{"loop": i["loop"], "field": i["field"], "problem": i["problem"]} for i in v.get("final_issues", [])],
        "before": v.get("before", {}),
        "sources": [s["url"] for s in r.get("sources", [])][:12],
        "human_override": r.get("human_override"),
        "_rounds_meta": [{"verifier_model": (rd.get("verifier") or {}).get("_model") or "unavailable"} for rd in v.get("rounds", [])],
        "_l4_skipped": bool(((v.get("rounds") or [{}])[0].get("verifier") or {}).get("error")),
    }


def apply_docs_check(recs: List[Dict[str, Any]]) -> None:
    """data/catalog_docs_check.csv: manual check of docs.composio.dev/toolkits/<slug> for apps the API listing missed."""
    p = DATA / "catalog_docs_check.csv"
    if not p.exists():
        return
    by_id = {r["id"]: r for r in recs}
    for row in csv.DictReader(open(p, newline="")):
        r = by_id.get(int(row["id"]))
        if not r or (r.get("composio_catalog") or {}).get("in_catalog"):
            continue
        r["composio_catalog"] = {"in_catalog": row["docs_page"] == "exists", "slug": row["slug_tried"],
                                 "via": "docs_page_check", "docs_page": row["docs_page"], "auth": []}


def apply_overrides(recs: List[Dict[str, Any]]) -> None:
    """data/human_overrides.csv: id,field,value,source_url,note  (value lists use ';')."""
    p = DATA / "human_overrides.csv"
    if not p.exists():
        return
    by_id = {r["id"]: r for r in recs}
    keymap = {"auth": "auth_methods", "access": "access_tier", "api": "api_types", "mcp": "mcp", "verdict": "verdict",
              "blocker": "blocker", "auth_primary": "auth_primary"}
    for row in csv.DictReader(open(p, newline="")):
        r = by_id.get(int(row["id"]))
        if not r:
            continue
        k = keymap.get(row["field"], row["field"])
        val: Any = row["value"].split(";") if k in ("auth_methods", "api_types") else row["value"]
        r.setdefault("human_override", []).append({"field": row["field"], "from": r.get(k), "to": val,
                                                   "source": row.get("source_url"), "note": row.get("note")})
        r[k] = val
        if row["field"] in r.get("needs_human", []):
            r["needs_human"].remove(row["field"])
        r.setdefault("field_status", {})[row["field"]] = "human_resolved"


def stats(recs: List[Dict[str, Any]], p1: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(recs)
    cats = list(dict.fromkeys(r["category"] for r in recs))
    s: Dict[str, Any] = {"n": n, "categories": cats}
    s["verdict"] = Counter(r["verdict"] for r in recs)
    s["access"] = Counter(r["access_tier"] for r in recs)
    s["auth_primary"] = Counter(r["auth_primary"] for r in recs)
    s["auth_any"] = Counter(m for r in recs for m in set(r["auth_methods"]))
    s["blocker"] = Counter(r["blocker"] for r in recs if r["blocker"] != "none")
    s["mcp"] = Counter(r["mcp"] for r in recs)
    grid: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    vgrid: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mgrid: Dict[str, int] = defaultdict(int)
    for r in recs:
        grid[r["category"]][r["access_tier"]] += 1
        vgrid[r["category"]][r["verdict"]] += 1
        mgrid[r["category"]] += int(r["mcp"] == "official")
    s["cat_access"] = {c: dict(grid[c]) for c in cats}
    s["cat_verdict"] = {c: dict(vgrid[c]) for c in cats}
    s["cat_mcp"] = dict(mgrid)
    in_cat = [r for r in recs if (r.get("composio_catalog") or {}).get("in_catalog")]
    s["in_catalog"] = len(in_cat)
    s["easy_wins_not_in_catalog"] = [r["name"] for r in recs if r["verdict"] == "build_now"
                                     and (r.get("composio_catalog") or {}).get("docs_page") == "404"]
    s["not_in_catalog_all"] = [r["name"] for r in recs if (r.get("composio_catalog") or {}).get("docs_page") == "404"]
    s["catalog_unchecked"] = [r["name"] for r in recs if not (r.get("composio_catalog") or {}).get("in_catalog")
                              and not (r.get("composio_catalog") or {}).get("docs_page")]
    s["outreach"] = [{"name": r["name"], "tier": r["access_tier"], "blocker": r["blocker"]} for r in recs
                     if r["access_tier"] in ("partner_or_sales", "approval_or_review")]
    # verification activity
    loops = Counter(i["loop"] for r in recs for i in r["issues_initial"])
    s["loops_initial"] = loops
    s["apps_flagged"] = sum(1 for r in recs if r["issues_initial"])
    s["apps_changed"] = sum(1 for r in recs if r["changed_fields"])
    s["fields_changed"] = Counter(f for r in recs for f in r["changed_fields"])
    s["needs_human_apps"] = [{"name": r["name"], "fields": r["needs_human"]} for r in recs if r["needs_human"]]
    st = Counter(v for r in recs for v in (r.get("field_status") or {}).values())
    s["field_status"] = st
    ev = [e for r in p1 for e in (r.get("evidence") or {}).values() if e and e.get("url") and e.get("quote")]
    s["p1_cited"] = len(ev)
    s["p1_quote_fail"] = sum(1 for r in recs for i in r["issues_initial"] if i["loop"] == "L1_evidence"
                             and "no evidence" not in i["problem"])
    s["p1_no_evidence"] = sum(1 for r in recs for i in r["issues_initial"] if i["loop"] == "L1_evidence"
                              and "no evidence" in i["problem"])
    s["verifier_models"] = Counter(rd.get("verifier_model") for r in recs for rd in r.get("_rounds_meta", []))
    s["l4_skipped_apps"] = [r["name"] for r in recs if r.get("_l4_skipped")]
    s["models_extract"] = Counter(r.get("_model") for r in p1)
    # pass-1 distribution for the before/after on the headline chart
    s["p1_verdict"] = Counter(r["verdict"] for r in p1)
    return json.loads(json.dumps(s))


def main() -> None:
    p2 = read_json(RUNS / "pass2.json")
    p1 = read_json(RUNS / "pass1.json")
    recs = [slim(r) for r in p2]
    apply_docs_check(recs)
    apply_overrides(recs)
    acc = read_json(DATA / "accuracy.json") if (DATA / "accuracy.json").exists() else {}
    insights = read_json(DATA / "insights.json") if (DATA / "insights.json").exists() else {}
    runinfo = read_json(DATA / "run_info.json") if (DATA / "run_info.json").exists() else {}
    s = stats(recs, p1)
    payload = {"apps": recs, "stats": s, "accuracy": acc, "insights": insights, "run": runinfo}
    SITE.mkdir(exist_ok=True)
    write_json(SITE / "results.json", {
        "schema": "one record per app; enums documented in agent/schema.py", "generated_from": "data/runs/pass2.json",
        "stats": s, "accuracy": acc.get("summary"), "apps": recs})
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    (SITE / "index.html").write_text(html)
    print(f"built docs/index.html ({len(html)//1024} KB) and docs/results.json for {len(recs)} apps")


if __name__ == "__main__":
    main()
