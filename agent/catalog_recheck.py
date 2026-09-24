"""L3b - late Composio catalog recheck (run after verify.py).

The bulk /toolkits listing used during pass 2 missed some toolkits (e.g. Twilio has a
toolkit page but was absent from the listing). This looks each unmatched app up by slug
with GET /toolkits/{slug}, records the match, and compares auth methods. It does NOT
change any answer: a disagreement is added to the human review queue instead.

    python agent/catalog_recheck.py
"""
from __future__ import annotations

import csv
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import composio_client as cc  # noqa: E402
from common import DATA, RUNS, log, read_json, write_json  # noqa: E402


def main() -> None:
    cat = cc.catalog(refresh=True)  # re-list with managed_by=all
    recs = sorted([read_json(p) for p in (RUNS / "pass2").glob("*.json")], key=lambda r: r["id"])
    found_late, disagreements = 0, 0
    for r in recs:
        v = r.setdefault("verification", {})
        info = v.get("catalog") or {}
        tk = cc.match_toolkit(r["name"], cat)
        via = "listing"
        if not tk:
            for slug in cc.candidate_slugs(r["name"]):
                tk = cc.get_toolkit(slug) or cc.get_toolkit(slug + "_mcp")
                if tk:
                    via = "slug_lookup"
                    break
        if not tk:
            # third view: the public docs page (the API listing omitted e.g. Aircall and Reducto)
            hit = next((sl for sl in cc.candidate_slugs(r["name"]) if cc.docs_page_exists(sl)), None)
            if hit:
                v["catalog"] = {"in_catalog": True, "slug": hit, "auth": [], "via": "docs_page"}
                found_late += 1
                write_json(RUNS / "pass2" / f"{r['id']:03d}.json", r)
            else:
                v["catalog"] = {"in_catalog": False, "checked": cc.candidate_slugs(r["name"])}
                write_json(RUNS / "pass2" / f"{r['id']:03d}.json", r)
            continue
        cat_auth = cc.catalog_auth_set(tk)
        was = info.get("in_catalog")
        v["catalog"] = {"in_catalog": True, "slug": tk["slug"], "auth": cat_auth, "tools_count": tk.get("tools_count"),
                        "via": via, "mcp_wrapper": str(tk["slug"]).endswith("_mcp")}
        if was:
            continue
        found_late += 1
        fam = lambda s: {("api_key" if x == "bearer_token" else x) for x in s} - {"other"}  # noqa: E731
        c, a = fam(cat_auth), fam(r["auth_methods"])
        if c and not (c & a):
            disagreements += 1
            issue = {"loop": "L3_catalog", "field": "auth",
                     "problem": f"late catalog check: agent auth {sorted(a)} vs Composio {tk['slug']} {cat_auth}"}
            v.setdefault("final_issues", []).append(issue)
            v.setdefault("initial_issues", []).append(issue)
            if "auth" not in v.setdefault("needs_human", []):
                v["needs_human"].append("auth")
                v.setdefault("field_status", {})["auth"] = "needs_human"
        write_json(RUNS / "pass2" / f"{r['id']:03d}.json", r)
    recs = sorted([read_json(p) for p in (RUNS / "pass2").glob("*.json")], key=lambda r: r["id"])
    write_json(RUNS / "pass2.json", recs)
    with open(DATA / "human_review_queue.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "field", "agent_value", "open_issues", "evidence_url"])
        for r in recs:
            for fld in r["verification"].get("needs_human", []):
                val = {"auth": r["auth_methods"], "access": r["access_tier"], "api": r["api_types"], "mcp": r["mcp"],
                       "verdict": r["verdict"]}[fld]
                probs = "; ".join(i["problem"] for i in r["verification"].get("final_issues", []) if i["field"] == fld)
                w.writerow([r["id"], r["name"], fld, val, probs, (r["evidence"].get(fld) or {}).get("url")])
    n_in = sum(1 for r in recs if r["verification"]["catalog"].get("in_catalog"))
    log(f"catalog recheck: {n_in}/{len(recs)} apps have a Composio toolkit ({found_late} found late, "
        f"{disagreements} new auth disagreements sent to human queue)")


if __name__ == "__main__":
    main()
