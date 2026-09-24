"""LLM calls over plain REST (no SDKs): Gemini (researcher) and Groq (verifier).

Two different model families on purpose: the verifier should not share the
researcher's blind spots. Either role can be swapped via .env:
  EXTRACTOR=gemini|groq   VERIFIER=groq|gemini
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional

import httpx

from common import RateLimiter, cache_get, cache_put, log, parse_json_loose, sha

_gem_rl = RateLimiter(float(os.getenv("GEMINI_RPM", "9")))
_groq_rl = RateLimiter(float(os.getenv("GROQ_RPM", "20")))
USAGE = {"gemini_calls": 0, "groq_calls": 0, "gemini_fallback": 0}
_MODELS: Dict[str, Any] = {}
_COOL: Dict[str, float] = {}


def _cooling(m: str) -> bool:
    return _COOL.get(m, 0) > time.time()


def _cool(m: str, secs: int, why: str) -> None:
    if not _cooling(m):
        log(f"  model {m} unavailable for {secs // 60} min ({why[:160]}) -> using fallback")
    _COOL[m] = time.time() + secs


def _ver(name: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return (float(m.group(1)) if m else 0.0, "preview" not in name and "exp" not in name)


def gemini_models() -> list:
    """Pick the newest available Flash model (+ a Flash-Lite fallback) from ListModels.

    Model names get retired often; discovering them avoids hard-coding a dead name.
    """
    if "gemini" in _MODELS:
        return _MODELS["gemini"]
    env = [m for m in (os.getenv("GEMINI_MODEL"), os.getenv("GEMINI_FALLBACK_MODEL")) if m]
    names: list = []
    try:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 1000},
                      headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY", "")}, timeout=30)
        for m in r.json().get("models", []):
            n = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if not n.startswith("gemini") or any(x in n for x in ("image", "tts", "live", "audio", "embedding", "vision", "thinking", "computer")):
                continue
            names.append(n)
    except Exception as e:  # noqa: BLE001
        log(f"  gemini model list failed: {e}")
    flash = sorted([n for n in names if "flash" in n and "lite" not in n], key=_ver, reverse=True)
    lite = sorted([n for n in names if "flash-lite" in n], key=_ver, reverse=True)
    picked = env + flash[:1] + lite[:1]
    if not picked:
        picked = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    _MODELS["gemini"] = list(dict.fromkeys(picked))
    return _MODELS["gemini"]


# Order = free-tier token budget first (verifier runs ~150 times), quality second.
GROQ_PREF = ["meta-llama/llama-4-scout-17b-16e-instruct", "openai/gpt-oss-120b", "meta-llama/llama-4-maverick-17b-128e-instruct",
             "qwen/qwen3-32b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]


def groq_model() -> str:
    return groq_models()[0]


def groq_models() -> list:
    if "groq" in _MODELS:
        return _MODELS["groq"]
    ids: list = []
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}"}, timeout=30)
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    except Exception as e:  # noqa: BLE001
        log(f"  groq model list failed: {e}")
    picked = [m for m in GROQ_PREF if m in ids]
    if not picked:
        picked = [m for m in ids if not any(x in m for x in ("whisper", "guard", "tts", "playai", "compound", "orpheus"))][:3]
    env = [os.getenv("GROQ_MODEL")] if os.getenv("GROQ_MODEL") else []
    _MODELS["groq"] = list(dict.fromkeys(env + picked)) or GROQ_PREF[:2]
    return _MODELS["groq"]


def gemini_json(prompt: str, system: str = "", model: Optional[str] = None) -> Dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing in .env")
    models = [model] if model else gemini_models()
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    last_err = ""
    for mi, m in enumerate(models):
        if _cooling(m) and mi < len(models) - 1:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        tries = 5 if mi == len(models) - 1 else 2
        for attempt in range(tries):
            _gem_rl.wait()
            try:
                r = httpx.post(url, json=body, headers={"x-goog-api-key": key}, timeout=180)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                USAGE["gemini_calls"] += 1
                if mi > 0:
                    USAGE["gemini_fallback"] += 1
                js = r.json()
                parts = js["candidates"][0]["content"].get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                out = parse_json_loose(text)
                out["_model"] = m
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (404, 403) or (r.status_code == 429 and ("PerDay" in r.text or "per day" in r.text.lower())):
                _cool(m, 3600, last_err)
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 8 * (attempt + 1)))
                continue
            break
        else:
            if mi < len(models) - 1:
                _cool(m, 600, last_err)
    raise RuntimeError(f"Gemini failed: {last_err}")


def groq_json(prompt: str, system: str = "", model: Optional[str] = None) -> Dict[str, Any]:
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY missing in .env")
    models = [model] if model else groq_models()
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    last_err = ""
    for mi, m in enumerate(models):
        if _cooling(m) and mi < len(models) - 1:
            continue
        body = {"model": m, "messages": msgs, "temperature": 0, "response_format": {"type": "json_object"}}
        for attempt in range(6):
            _groq_rl.wait()
            try:
                r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {key}"}, timeout=120)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(5)
                continue
            if r.status_code == 200:
                USAGE["groq_calls"] += 1
                out = parse_json_loose(r.json()["choices"][0]["message"]["content"])
                out["_model"] = m
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            low = r.text.lower()
            if r.status_code == 400 and "response_format" in body and ("json" in low or "response_format" in low):
                body.pop("response_format")  # some models reject JSON mode; the prompt still asks for JSON
                continue
            if r.status_code in (404, 403) or (r.status_code == 429 and ("per day" in low or "tpd" in low or "rpd" in low)):
                _cool(m, 3600, last_err)
                break
            if r.status_code in (429, 500, 502, 503):
                wait = r.headers.get("retry-after")
                time.sleep(min(90, float(wait) + 1 if wait else 10 * (attempt + 1)))
                continue
            break
    raise RuntimeError(f"Groq failed: {last_err}")


def call(role: str, prompt: str, system: str = "", cache_key: Optional[str] = None) -> Dict[str, Any]:
    """role: 'extractor' or 'verifier'. Responses cached so reruns cost nothing."""
    provider = os.getenv("EXTRACTOR", "gemini") if role == "extractor" else os.getenv("VERIFIER", "groq")
    ck = f"{provider}|{cache_key}|{sha(system + prompt)}" if cache_key else None
    if ck:
        c = cache_get("llm", ck)
        if c is not None:
            return c
    fn = gemini_json if provider == "gemini" else groq_json
    try:
        out = fn(prompt, system)
    except ValueError:  # malformed JSON from the model: ask once more
        out = fn(prompt + "\n\nReturn strictly valid JSON (double-quoted keys, no trailing commas).", system)
    if ck:
        cache_put("llm", ck, out)
    return out
