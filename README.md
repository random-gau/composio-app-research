# Composio app research agent

An agent that researches whether an app can become an agent toolkit. For each of 100 apps it records auth methods, self-serve vs gated access, API surface, MCP availability and a buildability verdict. Every answer cites a source URL and a verbatim quote. It then checks its own work with five verification loops and scores itself against a hand-checked sample.

**Live case study:** https://random-gau.github.io/composio-app-research/
**Machine-readable results:** [`docs/results.json`](docs/results.json)

## Results (run of 24 Sep 2026)

- **76 of 100** apps can be built today with free, self-serve credentials. 21 need a paid plan, trial, app review or local hosting; 3 are blocked.
- The most common blocker is a **paid plan or trial (12 of 24)**. Only 6 need a partnership or enterprise contract.
- **OAuth 2** is documented by 75 apps and is the main method for 56. 78 apps document two or more auth methods.
- **72** vendors publish their own MCP server.
- Accuracy on a hand-checked dev sample (19 apps × 5 fields): **75.8% after pass 1 → 81.1% after the verification loops (pass 2)**.
- **Pass 3** (targeted fixes designed on the dev sample) reached 86.3% on that sample but **stayed at 92.0% on a 10-app held-out test** it was never tuned on. It fixed one app and broke two. Treating that as overfitting, the final table stays on pass 2 and pass 3 is reported as an experiment.
- Hits and misses, including where the loops made an answer worse, are on the live page.

## How it works

```
apps.csv ─► PASS 1 (research.py) ─► pass1.json ─► PASS 2 (verify.py) ─► pass2.json ─► score.py ─► build_site.py ─► docs/
            search → fetch → trim → extract       L1 quote check           accuracy vs     docs/index.html + results.json
            (Composio search/fetch + Gemini)      L2 consistency rules     hand-checked
                                                  L3 Composio catalog      sample
                                                  L4 blind verifier (Groq)
                                                  L5 MCP probe
                                                  → targeted re-research → adjudicate (≤2 rounds)
                                                  → human review queue
```

| Step | Tool |
|---|---|
| Web search | Composio `COMPOSIO_SEARCH_WEB` (DuckDuckGo fallback) |
| Page fetch | direct HTTP, Composio `COMPOSIO_SEARCH_FETCH_URL_CONTENT` for JS-heavy pages |
| Extraction + adjudication | Gemini (`GEMINI_MODEL`, default `gemini-2.5-flash`) |
| Blind verifier | Groq Llama (`GROQ_MODEL`, default `llama-3.3-70b-versatile`), a different model family on purpose |
| Auth cross-check | Composio `/toolkits` catalog |

The labels (what counts as "self-serve", "blocked", etc.) are defined once in `agent/schema.py`. That makes the 100 answers comparable, and each field can be scored.

## Run it

Needs Python 3.9+ and three free keys: Gemini (aistudio.google.com), Groq (console.groq.com) and Composio (composio.dev).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install ddgs   # optional search fallback

cat > .env <<'EOF'
GEMINI_API_KEY=...
GROQ_API_KEY=...
COMPOSIO_API_KEY=...
EOF

python agent/check_setup.py              # preflight: tests every key and tool
python agent/research.py                 # pass 1, all 100 apps  (--ids 1,2,3 for a subset)
python agent/verify.py                   # pass 2, verification loops
python agent/catalog_recheck.py          # late Composio catalog check (slug lookups)
python agent/pass3.py                    # optional pass 3 experiment (L6 access check, L7 auth grounding, verdict rule)
python agent/score.py                    # accuracy of every pass on the dev sample and the held-out test
python agent/build_site.py               # writes docs/index.html and docs/results.json (GitHub Pages)
```

Every search, page and LLM response is cached in `data/cache/`, so re-runs are free and resumable. Delete `data/runs/pass2/` to re-run only the verification.

Optional `.env` settings: `EXTRACTOR=gemini|groq`, `VERIFIER=groq|gemini`, `GEMINI_RPM`, `GROQ_RPM`, `GEMINI_MODEL`, `GROQ_MODEL`.

## Where a human is involved

- `agent/schema.py`: the rubric that defines each label.
- `data/ground_truth_sample.csv`: dev sample, 20 apps (2 per category, seeded random draw) checked by hand against vendor docs. Borderline cases accept two labels, and the reason is noted.
- `data/ground_truth_test.csv`: held-out test, 10 different apps (1 per category, drawn after pass 3 was designed), checked the same way.
- `data/catalog_docs_check.csv`: apps the Composio `/toolkits` API did not return were checked by hand against `docs.composio.dev/toolkits/<slug>` (the API listing left out 7 toolkits that do have pages).
- `data/insights.json`: the pattern wording on the page, written after reading the results. Every number on the page is computed from the run data.
- `data/human_review_queue.csv`: fields the loops could not settle. Decisions go in `data/human_overrides.csv`. They are shown on the page as human-resolved and are not counted as agent accuracy.

## Files

```
agent/
  research.py        pass 1
  verify.py          pass 2 (loops L1–L5, re-research, adjudication)
  catalog_recheck.py late Composio catalog lookups by slug
  pass3.py           pass 3 experiment (L6 access/pricing check, L7 auth grounding, verdict rule)
  schema.py          vocabularies, rubric, prompts
  web.py             search + fetch + excerpting
  composio_client.py Composio REST (tool execution + toolkit catalog)
  llm.py             Gemini + Groq over REST, rate limiting, caching
  score.py           accuracy vs hand-checked sample
  build_site.py      case-study page generator (site_template.html)
data/
  apps.csv, ground_truth_sample.csv, runs/pass1.json, runs/pass2.json, accuracy.json
docs/
  index.html, results.json
```
