"""Shared helpers: paths, .env loading, caching, rate limiting, logging."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
RUNS = DATA / "runs"
for _p in (CACHE / "pages", CACHE / "search", CACHE / "llm", RUNS / "pass1", RUNS / "pass2"):
    _p.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Minimal .env loader so we need no python-dotenv dependency."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

_print_lock = threading.Lock()


def log(*parts: Any) -> None:
    with _print_lock:
        print(time.strftime("%H:%M:%S"), *parts, flush=True)


def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def cache_get(kind: str, key: str) -> Optional[Any]:
    p = CACHE / kind / f"{sha(key)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def cache_put(kind: str, key: str, value: Any) -> None:
    p = CACHE / kind / f"{sha(key)}.json"
    p.write_text(json.dumps(value, ensure_ascii=False))


def read_apps() -> List[Dict[str, str]]:
    with open(DATA / "apps.csv", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


class RateLimiter:
    """Spaces calls so we stay under a requests-per-minute budget (free tiers)."""

    def __init__(self, rpm: float):
        self.interval = 60.0 / max(rpm, 0.1)
        self.lock = threading.Lock()
        self.next_ok = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.time()
            if now < self.next_ok:
                time.sleep(self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + self.interval


def parse_json_loose(text: str) -> Any:
    """Parse model output that should be JSON, tolerating code fences / prose."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except Exception:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            t = t[start : end + 1]
        t = re.sub(r",\s*([}\]])", r"\1", t)  # trailing commas, a common small-model slip
        return json.loads(t)


def die(msg: str) -> None:
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)
