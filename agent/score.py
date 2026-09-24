"""Score pass 1 vs pass 2 against the hand-checked sample (data/ground_truth_sample.csv).

Scoring rules (kept deliberately simple and stated on the page):
  auth    - agent's primary method is in the documented set AND the agent lists no method the
            docs don't support. api_key and bearer_token count as one family (vendors use both
            names for the same static secret).
  access  - access_tier is one of the accepted labels (a few apps are genuinely borderline;
            both readings are accepted and the note says why).
  api     - agent's api_types overlap the accepted types.
  mcp     - label is in the accepted set.
  verdict - label is in the accepted set.
Blank truth cells are not scored (e.g. iPayX, where even the human could not establish the facts).

    python agent/score.py
"""
from __future__ import annotations

import csv
import sys
from typing import Any, Dict, List

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from common import DATA, RUNS, read_json, write_json  # noqa: E402

FIELDS = ["auth", "access", "api", "mcp", "verdict"]


def fam(x: str) -> str:
    return "api_key" if x == "bearer_token" else x


def judge(rec: Dict[str, Any], t: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if t["auth_methods"]:
        truth = {fam(x) for x in t["auth_methods"].split(";")} | {"other"} if "other" in t["auth_methods"] else \
            {fam(x) for x in t["auth_methods"].split(";")}
        agent = {fam(x) for x in rec["auth_methods"]}
        out["auth"] = fam(rec["auth_primary"]) in truth and agent <= (truth | {"other"})
        out["_auth_primary"] = fam(rec["auth_primary"]) in truth  # secondary, lenient metric (not in overall)
    if t["access_accept"]:
        out["access"] = rec["access_tier"] in t["access_accept"].split("|")
    if t["api_accept"]:
        out["api"] = bool(set(rec["api_types"]) & set(t["api_accept"].split("|")))
    if t["mcp_accept"]:
        out["mcp"] = rec["mcp"] in t["mcp_accept"].split("|")
    if t["verdict_accept"]:
        out["verdict"] = rec["verdict"] in t["verdict_accept"].split("|")
    return out


def score_set(truth_file: str) -> Dict[str, Any]:
    truth = list(csv.DictReader(open(DATA / truth_file, newline="")))
    passes = {}
    for name in ("pass1", "pass2", "pass3"):
        p = RUNS / f"{name}.json"
        if p.exists():
            passes[name] = {r["id"]: r for r in read_json(p)}
    rows: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}
    for name, recs in passes.items():
        tot = {f: [0, 0] for f in FIELDS}
        prim = [0, 0]
        for t in truth:
            rec = recs.get(int(t["id"]))
            if not rec:
                continue
            j = judge(rec, t)
            for f, ok in j.items():
                if f == "_auth_primary":
                    prim[0] += int(ok)
                    prim[1] += 1
                    continue
                tot[f][0] += int(ok)
                tot[f][1] += 1
            rows.append({"pass": name, "id": int(t["id"]), "name": t["name"], **{f: j.get(f) for f in FIELDS},
                         "agent": {"auth": rec["auth_methods"], "access": rec["access_tier"], "api": rec["api_types"],
                                   "mcp": rec["mcp"], "verdict": rec["verdict"]}})
        correct = sum(v[0] for v in tot.values())
        n = sum(v[1] for v in tot.values())
        summary[name] = {"per_field": {f: {"correct": c, "n": k, "pct": round(100 * c / k, 1) if k else None}
                                       for f, (c, k) in tot.items()},
                         "overall": {"correct": correct, "n": n, "pct": round(100 * correct / n, 1) if n else None},
                         "auth_primary_only": {"correct": prim[0], "n": prim[1],
                                               "pct": round(100 * prim[0] / prim[1], 1) if prim[1] else None}}
    return {"summary": summary, "rows": rows, "truth": truth}


def main() -> None:
    dev = score_set("ground_truth_sample.csv")
    out = dict(dev)
    if (DATA / "ground_truth_test.csv").exists():
        test = score_set("ground_truth_test.csv")
        out.update({"test_summary": test["summary"], "test_rows": test["rows"], "test_truth": test["truth"]})
    write_json(DATA / "accuracy.json", out)
    report("DEV sample (used to design pass 3)", dev)
    if "test_summary" in out:
        report("HELD-OUT test sample (never used for design)", {"summary": out["test_summary"], "rows": out["test_rows"]})


def report(title: str, res: Dict[str, Any]) -> None:
    summary, rows = res["summary"], res["rows"]
    print(f"== {title}")
    for name, s in summary.items():
        pf = "  ".join(f"{f}={v['correct']}/{v['n']}" for f, v in s["per_field"].items())
        print(f"{name}: overall {s['overall']['correct']}/{s['overall']['n']} = {s['overall']['pct']}%   {pf}"
              f"   (primary auth only: {s['auth_primary_only']['correct']}/{s['auth_primary_only']['n']})")
    # list the misses so a human can inspect them
    for r in rows:
        bad = [f for f in FIELDS if r.get(f) is False]
        if bad:
            print(f"  miss {r['pass']} #{r['id']} {r['name']}: {bad} agent={ {f: r['agent'][f] for f in bad} }")


if __name__ == "__main__":
    main()
