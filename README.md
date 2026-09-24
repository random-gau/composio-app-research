# Composio app research agent

An agent that researches whether an app can become an agent toolkit. For each of 100 apps it records auth methods, self-serve vs gated access, API surface, MCP availability and a buildability verdict. Every answer cites a source URL and a verbatim quote. It then checks its own work with five verification loops and scores itself against a hand-checked sample.

**Live case study:** https://random-gau.github.io/composio-app-research/
**Machine-readable results:** [`site/results.json`](site/results.json)

## How it works

```
apps.csv ─► PASS 1 (research.py) ─► pass1.json ─► PASS 2 (verify.py) ─► pass2.json ─► score.py ─► build_site.py ─► site/
            search → fetch → trim → extract       L1 quote check           accuracy vs     index.html + results.json
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
python agent/score.py                    # accuracy of pass 1 vs pass 2 on the hand-checked sample
python agent/build_site.py               # writes site/index.html and site/results.json
```

Every search, page and LLM response is cached in `data/cache/`, so re-runs are free and resumable. Delete `data/runs/pass2/` to re-run only the verification.

Optional `.env` settings: `EXTRACTOR=gemini|groq`, `VERIFIER=groq|gemini`, `GEMINI_RPM`, `GROQ_RPM`, `GEMINI_MODEL`, `GROQ_MODEL`.

## Where a human is involved

- `agent/schema.py`: the rubric that defines each label.
- `data/ground_truth_sample.csv`: 20 apps (2 per category, seeded random draw) checked by hand against vendor docs. Borderline cases accept two labels, and the reason is noted.
- `data/human_review_queue.csv`: fields the loops could not settle. Decisions go in `data/human_overrides.csv`. They are shown on the page as human-resolved and are not counted as agent accuracy.

## Files

```
agent/
  research.py        pass 1
  verify.py          pass 2 (loops L1–L5, re-research, adjudication)
  schema.py          vocabularies, rubric, prompts
  web.py             search + fetch + excerpting
  composio_client.py Composio REST (tool execution + toolkit catalog)
  llm.py             Gemini + Groq over REST, rate limiting, caching
  score.py           accuracy vs hand-checked sample
  build_site.py      case-study page generator (site_template.html)
data/
  apps.csv, ground_truth_sample.csv, runs/pass1.json, runs/pass2.json, accuracy.json
site/
  index.html, results.json
```
