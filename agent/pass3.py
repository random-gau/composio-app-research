"""PASS 3 - targeted fixes for the three error patterns found in pass 2.

Written after reading pass 2's misses on the dev sample (data/ground_truth_sample.csv), and
then scored on a NEW held-out sample (data/ground_truth_test.csv) that played no part in
designing these fixes. That is how we know the fixes generalise rather than fit the sample.

  L6 access check  - search + read the app's pricing / API-access pages and classify the access
                     gate with a verbatim quote. Stricter sandbox rule: a sandbox only counts as
                     self-serve if ANY developer can sign up for it (not only existing customers).
                     Also asks whether public/multi-user use needs an app review.
  L7 auth grounding- every listed auth method must have its own verbatim quote showing the PUBLIC
                     API accepts it for API requests; methods whose quote is missing or not on the
                     page are dropped (e.g. OAuth used only for Plaid Link, Basic used only for git).
  Verdict rule     - verdict is derived from the access gate instead of being a separate LLM
                     guess, so it can never contradict the access answer.

    python agent/pass3.py [--ids 1,2] [--workers 3]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import llm  # noqa: E402
import web  # noqa: E402
from common import RUNS, log, read_apps, read_json, write_json  # noqa: E402
from research import pick_urls  # noqa: E402
from schema import ACCESS, AUTH, normalize, sources_block  # noqa: E402
from verify import quote_on_page  # noqa: E402

(RUNS / "pass3").mkdir(parents=True, exist_ok=True)

ACCESS_SYSTEM = (
    "You decide how a developer can get working API credentials for an app. Use ONLY the sources. "
    "Quote verbatim (6-40 words) from the source you rely on. Never invent a URL or quote."
)

ACCESS_PROMPT = """APP: {name} ({category})
Classify access_tier with these exact rules:
  self_serve_free    - any developer can sign up and get API credentials at $0 (free plan, free developer account, or a
                       free sandbox/test mode that ANYONE can sign up for).
  self_serve_trial   - API credentials only during a time-limited free trial; continued API use needs a paid plan.
  paid_plan          - API access only on a paid plan / tier (e.g. "API available on Pro and above", "only stores on paid plans").
  approval_or_review - you must apply or be approved first (developer application, partner/app review, token approval).
  partner_or_sales   - access only for existing enterprise customers, via partnership, or by contacting sales.
                       A sandbox that is only provisioned for existing customers is partner_or_sales, NOT self-serve.
  open_source_local  - open-source tool you run yourself; no hosted API credentials.
Also answer: do the sources SAY that using the API for other users' accounts (a public / multi-user app) requires an
app review or approval? Answer "yes" only with a verbatim quote that says so; otherwise "no" or "unknown".
Return ONLY JSON:
{{"access_tier": "...", "public_use_needs_review": "yes|no|unknown",
  "review_evidence": {{"url": "SOURCE url or null", "quote": "verbatim or null"}},
  "evidence": {{"url": "one of the SOURCE urls", "quote": "verbatim"}}, "reason": "one sentence"}}
SOURCES:
{sources}"""

AUTH_PROMPT = """APP: {name}
Currently listed auth methods: {methods} (primary: {primary})
For EACH method, find a verbatim quote (6-40 words) in the sources showing that the app's PUBLIC REST/GraphQL API accepts
it to authenticate API requests. Do NOT count: OAuth used only for end-user bank/account linking widgets, Basic auth used
only for git operations, JWTs used only by client-side SDKs, or auth used only by an MCP server.
Methods allowed: {allowed}. You may also ADD a method if a quote shows the API accepts it.
Return ONLY JSON:
{{"methods": [{{"method": "...", "url": "SOURCE url or null", "quote": "verbatim or null"}}], "primary": "..."}}
SOURCES:
{sources}"""


def derive_verdict(rec: Dict[str, Any]) -> None:
    a, api = rec["access_tier"], rec["api_types"]
    no_api = api in (["none"], ["sdk_or_cli_only"])
    if a == "open_source_local":
        rec["verdict"], rec["blocker"] = "build_with_friction", "local_tool_only"
    elif no_api and a != "open_source_local":
        rec["verdict"], rec["blocker"] = "blocked", "no_public_api"
    elif a == "partner_or_sales":
        rec["verdict"] = "blocked"
        rec["blocker"] = rec["blocker"] if rec["blocker"] in ("partner_approval", "enterprise_only") else "partner_approval"
    elif a in ("paid_plan", "self_serve_trial"):
        rec["verdict"], rec["blocker"] = "build_with_friction", "paid_plan"
    elif a == "approval_or_review" or rec.get("_public_review") == "yes":
        rec["verdict"], rec["blocker"] = "build_with_friction", "app_review"
    elif a == "self_serve_free":
        rec["verdict"], rec["blocker"] = "build_now", "none"


def pages_for(app: Dict[str, str], queries: List[str], limit: int = 5) -> List[Dict[str, Any]]:
    results: List[Dict[str, str]] = []
    for q in queries:
        results += web.search(q, n=8)
    out = []
    for u in pick_urls(app, results, limit + 3):
        p = web.fetch(u)
        if len(p.get("text", "")) < 200:
            continue
        out.append({"url": u, "excerpt": web.excerpt(p["text"], 3000)})
        if len(out) >= limit:
            break
    return out


def run_app(app: Dict[str, str], force: bool = False) -> Dict[str, Any]:
    out_path = RUNS / "pass3" / f"{int(app['id']):03d}.json"
    if out_path.exists() and not force:
        return read_json(out_path)
    rec = normalize(dict(read_json(RUNS / "pass2" / f"{int(app['id']):03d}.json")))
    before = {"auth_methods": list(rec["auth_methods"]), "auth_primary": rec["auth_primary"],
              "access_tier": rec["access_tier"], "verdict": rec["verdict"], "blocker": rec["blocker"]}
    name = re.sub(r"\(.*?\)", "", app["name"]).strip()
    log3: List[Dict[str, Any]] = []

    # ---- L6 access / pricing check
    acc_pages = pages_for(app, [f"{name} pricing plans API access", f"{name} API access requirements developer sign up"])
    try:
        a = llm.call("extractor", ACCESS_PROMPT.format(name=app["name"], category=app["category"],
                                                       sources=sources_block(acc_pages)),
                     ACCESS_SYSTEM, cache_key=f"p3acc|{app['id']}")
        tier = str(a.get("access_tier", "")).strip().lower()
        ev = a.get("evidence") or {}
        ok = False
        if tier in ACCESS and ev.get("url") and ev.get("quote"):
            st, _ = quote_on_page(ev["quote"], ev["url"])
            ok = st == "ok"
        rec["_public_review"] = "unknown"
        rv = a.get("review_evidence") or {}
        if str(a.get("public_use_needs_review", "")).lower() == "yes" and rv.get("url") and rv.get("quote"):
            if quote_on_page(rv["quote"], rv["url"])[0] == "ok":  # only a verified quote can add a review gate
                rec["_public_review"] = "yes"
                rec["evidence"]["review"] = {"url": rv["url"], "quote": rv["quote"], "check": "ok"}
        if ok and tier != rec["access_tier"]:
            log3.append({"loop": "L6_access", "field": "access", "change": f"{rec['access_tier']} -> {tier}",
                         "reason": a.get("reason"), "url": ev["url"]})
            rec["access_tier"] = tier
            rec["access_notes"] = a.get("reason") or rec.get("access_notes")
            rec["evidence"]["access"] = {"url": ev["url"], "quote": ev["quote"], "check": "ok"}
        elif not ok:
            log3.append({"loop": "L6_access", "field": "access", "change": None,
                         "reason": f"L6 answer '{tier}' not adopted: quote not verified on page"})
        else:
            log3.append({"loop": "L6_access", "field": "access", "change": None, "reason": "confirmed with a verified quote"})
    except Exception as e:  # noqa: BLE001
        log3.append({"loop": "L6_access", "error": str(e)[:200]})

    # ---- L7 auth grounding
    auth_pages = [{"url": s["url"], "excerpt": web.excerpt(web.fetch(s["url"]).get("text", ""), 2500)}
                  for s in rec.get("sources", [])[:5]]
    auth_pages = [p for p in auth_pages if p["excerpt"]]
    try:
        r = llm.call("extractor", AUTH_PROMPT.format(name=app["name"], methods=rec["auth_methods"],
                                                     primary=rec["auth_primary"], allowed=", ".join(AUTH),
                                                     sources=sources_block(auth_pages)),
                     "Ground every auth method in a verbatim quote from the sources. Never invent a URL or quote.",
                     cache_key=f"p3auth|{app['id']}")
        kept, dropped = [], []
        for m in r.get("methods") or []:
            meth = normalize({"auth_methods": [m.get("method")]})["auth_methods"][0]
            if meth == "other" and str(m.get("method", "")).lower() != "other":
                continue
            good = False
            if m.get("url") and m.get("quote"):
                good = quote_on_page(m["quote"], m["url"])[0] == "ok"
            (kept if good else dropped).append(meth)
        if rec["auth_methods"] == ["none"]:
            kept = ["none"]
        if kept:
            prim = normalize({"auth_methods": kept, "auth_primary": r.get("primary")})["auth_primary"]
            new = list(dict.fromkeys(kept))
            if set(new) != set(rec["auth_methods"]):
                log3.append({"loop": "L7_auth", "field": "auth", "change": f"{rec['auth_methods']} -> {new}",
                             "reason": f"dropped (no verified quote): {sorted(set(rec['auth_methods']) - set(new))}"})
            rec["auth_methods"], rec["auth_primary"] = new, prim if prim in new else new[0]
        else:
            log3.append({"loop": "L7_auth", "field": "auth", "change": None,
                         "reason": "no method could be grounded; pass 2 answer kept"})
    except Exception as e:  # noqa: BLE001
        log3.append({"loop": "L7_auth", "error": str(e)[:200]})

    # ---- deterministic verdict
    derive_verdict(rec)
    if rec["verdict"] != before["verdict"]:
        log3.append({"loop": "verdict_rule", "field": "verdict", "change": f"{before['verdict']} -> {rec['verdict']}",
                     "reason": f"derived from access {rec['access_tier']} / public review {rec.get('_public_review')}"})
    rec["pass3"] = {"before": before, "log": log3, "access_sources": [p["url"] for p in acc_pages]}
    rec["sources"] = rec.get("sources", []) + [{"url": u, "via": "pass3"} for u in rec["pass3"]["access_sources"]]
    write_json(out_path, rec)
    ch = [x["field"] for x in log3 if x.get("change")]
    log(f"[pass3] {app['id']:>3} {app['name']:<28} changed={ch or '-'}")
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
    t0 = time.time()
    log(f"pass3: {len(apps)} apps")
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(lambda x: run_app(x, a.force), apps))
    recs = sorted([read_json(p) for p in (RUNS / "pass3").glob("*.json")], key=lambda r: r["id"])
    write_json(RUNS / "pass3.json", recs)
    log(f"pass3 done in {round((time.time() - t0) / 60, 1)} min. llm={llm.USAGE} web={web.STATS}")


if __name__ == "__main__":
    main()
