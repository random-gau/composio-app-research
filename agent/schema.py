"""Output schema, controlled vocabularies, rubric and prompts.

Controlled vocabularies (not free text) are what make 100 rows clusterable and
make accuracy measurable field-by-field.
"""
from __future__ import annotations

from typing import Any, Dict, List

AUTH = ["oauth2", "api_key", "bearer_token", "basic", "jwt_or_signed", "none", "other"]
ACCESS = [
    "self_serve_free",      # any dev can sign up and get working creds at $0 (free plan, dev account, sandbox)
    "self_serve_trial",     # creds only during a time-limited trial; ongoing use needs payment
    "paid_plan",            # API only on a paid tier, but you can buy it yourself
    "approval_or_review",   # must pass app review / developer application / token approval
    "partner_or_sales",     # partnership, contact-sales, or enterprise contract only
    "open_source_local",    # OSS tool/library you run yourself; no hosted API creds
    "unknown",
]
API_TYPES = ["rest", "graphql", "soap", "websocket", "sdk_or_cli_only", "none"]
BREADTH = ["broad", "moderate", "narrow", "none", "unknown"]
MCP = ["official", "community_only", "none_found"]
VERDICT = ["build_now", "build_with_friction", "blocked"]
BLOCKER = [
    "none", "paid_plan", "partner_approval", "app_review", "no_public_api",
    "local_tool_only", "enterprise_only", "docs_unclear",
]
FIELDS = ["auth", "access", "api", "mcp", "verdict"]

RUBRIC = """
DEFINITIONS (apply exactly):
auth_methods: every auth method the app's PUBLIC API documents, from
  oauth2 | api_key | bearer_token (static personal/access token you paste) | basic (username+password or key-as-basic)
  | jwt_or_signed (JWT assertion, HMAC-signed requests, service-account keys) | none | other.
  auth_primary = the method a third-party integration (like Composio) would normally use.
access_tier: what gate a developer hits to get WORKING credentials for their own account/sandbox:
  self_serve_free   - sign up and get credentials at $0 (free plan, free developer account, free sandbox/test mode).
  self_serve_trial  - credentials only during a time-limited free trial; continued use requires paying.
  paid_plan         - API only on a paid tier/plan or pay-as-you-go with no free usage; you can buy it yourself.
  approval_or_review- you must be approved first: developer application, app review, developer-token approval, allow-listing.
  partner_or_sales  - only via partnership, contact sales, or enterprise contract.
  open_source_local - open-source tool/library you run locally; no hosted API or credentials.
  (If a free sandbox exists but production needs review, choose self_serve_free and mention the production gate in access_notes.)
api_types: rest | graphql | soap | websocket | sdk_or_cli_only | none. api_breadth: broad (>50 endpoints / most product objects),
  moderate (10-50), narrow (<10), none.
mcp: official = an MCP server published or documented by the vendor itself; community_only = only third-party MCP servers; none_found.
verdict: build_now = public docs + self-serve credentials, a toolkit could be built today;
  build_with_friction = buildable but needs a paid plan, trial, app review or other hurdle;
  blocked = no public API or partnership/enterprise-only, can't build today.
blocker: none | paid_plan | partner_approval | app_review | no_public_api | local_tool_only | enterprise_only | docs_unclear
"""

OUTPUT_SHAPE = """
Return ONLY this JSON object:
{
 "one_liner": "what the product does, <=15 words",
 "auth_methods": ["oauth2", ...],
 "auth_primary": "oauth2",
 "auth_notes": "short",
 "access_tier": "self_serve_free",
 "access_notes": "short: what exactly is required",
 "api_types": ["rest"],
 "api_breadth": "broad",
 "api_notes": "short: resources covered / rough endpoint count / base URL",
 "mcp": "official",
 "mcp_url": "url or null",
 "verdict": "build_now",
 "blocker": "none",
 "blocker_notes": "short",
 "evidence": {
   "auth":    {"url": "one of the SOURCE urls or null", "quote": "verbatim sentence copied from that source or null"},
   "access":  {"url": null, "quote": null},
   "api":     {"url": null, "quote": null},
   "mcp":     {"url": null, "quote": null},
   "verdict": {"url": null, "quote": null}
 },
 "confidence": {"auth": 0.0, "access": 0.0, "api": 0.0, "mcp": 0.0, "verdict": 0.0}
}
"""

EXTRACT_SYSTEM = (
    "You are an API integration researcher at Composio, deciding whether each app can become an agent toolkit. "
    "Ground every field in the SOURCES provided. For each evidence entry, url MUST be one of the SOURCE urls and "
    "quote MUST be copied verbatim (character for character, 6-40 words) from that source's text. "
    "If the sources do not support a field, give your best belief, set that evidence url/quote to null and lower confidence. "
    "Never invent a URL or a quote."
)


def sources_block(pages: List[Dict[str, Any]]) -> str:
    out = []
    for i, p in enumerate(pages, 1):
        out.append(f"### SOURCE {i}\nurl: {p['url']}\n---\n{p['excerpt']}\n")
    return "\n".join(out)


def extract_prompt(app: Dict[str, str], pages: List[Dict[str, Any]]) -> str:
    return (
        f"APP: {app['name']}  (category: {app['category']}; hint: {app['hint']})\n"
        f"{RUBRIC}\n{OUTPUT_SHAPE}\nSOURCES:\n{sources_block(pages)}"
    )


VERIFY_SYSTEM = (
    "You are an independent fact-checker. Answer ONLY from the sources given. You have not seen anyone else's answer. "
    "If the sources don't settle a field, say 'unknown' for it."
)

VERIFY_SHAPE = """
Return ONLY JSON:
{"auth_methods": [...], "auth_primary": "...", "access_tier": "...", "api_types": [...], "mcp": "official|community_only|none_found|unknown",
 "verdict": "build_now|build_with_friction|blocked|unknown", "reasons": {"auth": "...", "access": "...", "mcp": "...", "verdict": "..."}}
"""


def verify_prompt(app: Dict[str, str], pages: List[Dict[str, Any]]) -> str:
    return f"APP: {app['name']} ({app['category']})\n{RUBRIC}\n{VERIFY_SHAPE}\nSOURCES:\n{sources_block(pages)}"


ADJUDICATE_SYSTEM = (
    "You are the senior reviewer. A first-pass research answer was flagged by automated checks. "
    "Using ONLY the sources (old and newly gathered), decide the correct values for the flagged fields. "
    "Cite a SOURCE url and a verbatim quote for each decision. If the evidence still does not settle it, keep "
    "the best-supported value and set needs_human=true for that field."
)


def adjudicate_prompt(app: Dict[str, str], record: Dict[str, Any], issues: List[Dict[str, Any]],
                      verifier: Dict[str, Any], pages: List[Dict[str, Any]]) -> str:
    import json

    first = {k: record.get(k) for k in ["auth_methods", "auth_primary", "access_tier", "access_notes", "api_types",
                                          "api_breadth", "mcp", "mcp_url", "verdict", "blocker"]}
    flagged = sorted({i["field"] for i in issues})
    return (
        f"APP: {app['name']} ({app['category']})\n{RUBRIC}\n"
        f"FIRST-PASS ANSWER: {json.dumps(first)}\n"
        f"INDEPENDENT VERIFIER ANSWER: {json.dumps({k: v for k, v in verifier.items() if not k.startswith('_')})}\n"
        f"CHECK FAILURES: {json.dumps(issues)}\n"
        f"FLAGGED FIELDS: {flagged}\n"
        "Return ONLY JSON with the same keys as the first-pass answer that you are (re)deciding, plus:\n"
        '"evidence": {"<field>": {"url": "...", "quote": "..."}}, "needs_human": {"<field>": true/false}, '
        '"change_log": "one sentence: what changed and why"\n'
        f"SOURCES:\n{sources_block(pages)}"
    )


def normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce model output onto the controlled vocabularies."""
    def pick(v: Any, allowed: List[str], default: str) -> str:
        v = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return v if v in allowed else default

    def picks(v: Any, allowed: List[str]) -> List[str]:
        if isinstance(v, str):
            v = [v]
        out = []
        for x in v or []:
            x = str(x).strip().lower().replace("-", "_").replace(" ", "_")
            x = {"oauth": "oauth2", "oauth_2": "oauth2", "oauth2.0": "oauth2", "apikey": "api_key", "token": "bearer_token",
                 "personal_access_token": "bearer_token", "jwt": "jwt_or_signed", "hmac": "jwt_or_signed"}.get(x, x)
            if x in allowed and x not in out:
                out.append(x)
        return out

    rec["auth_methods"] = picks(rec.get("auth_methods"), AUTH) or ["other"]
    rec["auth_primary"] = pick(rec.get("auth_primary"), AUTH, rec["auth_methods"][0])
    if rec["auth_primary"] not in rec["auth_methods"]:
        rec["auth_methods"].insert(0, rec["auth_primary"])
    rec["access_tier"] = pick(rec.get("access_tier"), ACCESS, "unknown")
    rec["api_types"] = picks(rec.get("api_types"), API_TYPES) or ["none"]
    rec["api_breadth"] = pick(rec.get("api_breadth"), BREADTH, "unknown")
    rec["mcp"] = pick(rec.get("mcp"), MCP, "none_found")
    rec["verdict"] = pick(rec.get("verdict"), VERDICT, "build_with_friction")
    rec["blocker"] = pick(rec.get("blocker"), BLOCKER, "docs_unclear")
    ev = rec.get("evidence") or {}
    rec["evidence"] = {f: (ev.get(f) if isinstance(ev.get(f), dict) else {"url": None, "quote": None}) for f in FIELDS}
    return rec
