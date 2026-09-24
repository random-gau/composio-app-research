"""Preflight: confirms each key works before spending quota on 100 apps.

    python agent/check_setup.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import composio_client  # noqa: E402
import llm  # noqa: E402
import web  # noqa: E402

ok = True


def step(name, fn):
    global ok
    try:
        out = fn()
        print(f"[OK]   {name}: {out}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] {name}: {str(e)[:300]}")


for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "COMPOSIO_API_KEY"):
    print(f"{k}: {'set' if os.getenv(k) else 'MISSING'}")
ck = os.getenv("COMPOSIO_API_KEY", "")
if ck.startswith("ck_"):
    print("NOTE: COMPOSIO_API_KEY starts with ck_ (a consumer/Connect key). The agent needs a Platform project "
          "API key, which starts with ak_. Get it from platform.composio.dev -> your project -> Settings -> API Keys.")
step("Gemini models picked", lambda: llm.gemini_models())
step("Groq models picked", lambda: llm.groq_models())

step("Composio search tool", lambda: [r["url"] for r in web.search("Stripe API authentication docs", n=3)] or "no results")
step("Composio fetch tool", lambda: str(composio_client.execute("COMPOSIO_SEARCH_FETCH_URL_CONTENT", {"url": "https://docs.stripe.com/api/authentication"}))[:120])
step("Composio toolkit catalog", lambda: f"{len(composio_client.catalog(refresh=True))} toolkits")
step("direct page fetch", lambda: f"{len(web.fetch('https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api')['text'])} chars")
step("Gemini (researcher)", lambda: json.dumps(llm.gemini_json('Return JSON {"ok": true}')))
step("Groq (verifier)", lambda: json.dumps(llm.groq_json('Return JSON {"ok": true}')))
print("\nALL GOOD - run: python agent/research.py --ids 81,71,58" if ok else "\nFix the FAIL lines above, then re-run.")
