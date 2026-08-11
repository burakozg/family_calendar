"""Daily AI token accounting. Every completion appends one line to
data/ai_usage.jsonl; the admin AI panel reads back per-day totals.

WHY APPEND-ONLY, not a running counter in a JSON store: a counter is a
read-modify-write, and the global write lock (storage._write_lock) is deliberately
NOT held across AI calls — two completions finishing together would lose an
update. An append needs no lock, and the raw lines stay useful for "what did that
recipe import actually cost?" in a way a bare counter never is.

Tokens are always recorded. `cost` only when the provider reports one: OpenRouter
returns the real credit cost of every call, while Anthropic/OpenAI/Mistral return
tokens alone. This app's model registry deliberately tracks relative cost tiers
(1–4), not prices, so there is no price table here to multiply tokens by — one
that silently went stale would be worse than no number at all. Tokens are the
honest common denominator; cost is a bonus where it's authoritative.

Only calls that actually consumed tokens are recorded — a request rejected for a
missing key or an exhausted quota never reaches a provider's meter, and counting
it would inflate the day's call count with zero-token rows."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import config  # noqa: F401  (loads .env before the DATA_DIR read below)
from fsatomic import _atomic_write_text

USAGE_FILE   = Path(os.getenv("DATA_DIR", "/data")) / "ai_usage.jsonl"
RETAIN_DAYS  = 120           # how much history the file keeps
TRIM_BYTES   = 1_000_000     # trim once the file grows past this


def _usage_from(provider: str, result: dict) -> tuple[int, int, float | None]:
    """(input tokens, output tokens, cost or None) from a provider's `usage`.
    Anthropic names them input_/output_tokens; everyone else uses the OpenAI
    prompt_/completion_tokens. Verified against live responses from all four."""
    u = (result or {}).get("usage") or {}
    if not isinstance(u, dict):
        return (0, 0, None)
    cost = u.get("cost")                     # OpenRouter reports real credits spent
    if provider == "anthropic":
        # Cache reads/writes are billed input too. This app sends no cache_control
        # blocks so they're 0 today, but summing keeps the number true if it ever does.
        tin = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
            + (u.get("cache_read_input_tokens") or 0)
        return (tin, u.get("output_tokens") or 0, cost if isinstance(cost, (int, float)) else None)
    return (u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0,
            cost if isinstance(cost, (int, float)) else None)


def record(model: dict, action: str, result: dict) -> None:
    """Append one usage line. Never raises: accounting must not be able to fail
    an AI feature that already produced a good answer."""
    try:
        tin, tout, cost = _usage_from(model["provider"], result)
        if not (tin or tout):
            return                            # nothing metered — don't log a zero row
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "model": model["id"], "provider": model["provider"], "action": action,
                 "in": tin, "out": tout}
        if cost is not None:
            entry["cost"] = cost
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if USAGE_FILE.stat().st_size > TRIM_BYTES:
            _trim()
    except Exception:
        pass


def _trim() -> None:
    """Drop lines older than RETAIN_DAYS. Rewritten atomically, so a crash
    mid-trim can't leave a half-written ledger."""
    cutoff = (datetime.now() - timedelta(days=RETAIN_DAYS)).isoformat(timespec="seconds")
    kept = [ln for ln in USAGE_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and _ts_of(ln) >= cutoff]
    _atomic_write_text(USAGE_FILE, "\n".join(kept) + "\n" if kept else "")


def _ts_of(line: str) -> str:
    try:
        return json.loads(line).get("ts", "")
    except Exception:
        return ""


def _read() -> list[dict]:
    if not USAGE_FILE.exists():
        return []
    out = []
    for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if isinstance(e, dict) and e.get("ts"):
            out.append(e)
    return out


def _blank(date: str) -> dict:
    return {"date": date, "calls": 0, "in": 0, "out": 0, "total": 0, "cost": 0.0, "cost_known": True}


def summary(days: int = 14) -> dict:
    """Per-day totals (newest first) over the last `days` local days, plus a
    per-model breakdown for the same window.

    `cost_known` is False for any bucket containing a call whose provider didn't
    report a cost — the money figure is then a floor, not a total, and the UI says
    so rather than quietly under-reporting."""
    days = max(1, min(days, RETAIN_DAYS))
    today = datetime.now().date()
    first = (today - timedelta(days=days - 1)).isoformat()
    buckets = {(today - timedelta(days=i)).isoformat(): _blank((today - timedelta(days=i)).isoformat())
               for i in range(days)}
    models: dict[tuple, dict] = {}
    for e in _read():
        date = e["ts"][:10]
        if date < first or date not in buckets:
            continue
        tin, tout, cost = e.get("in") or 0, e.get("out") or 0, e.get("cost")
        for b in (buckets[date],
                  models.setdefault((e.get("model"), e.get("provider")),
                                    {"model": e.get("model"), "provider": e.get("provider"),
                                     "calls": 0, "in": 0, "out": 0, "total": 0,
                                     "cost": 0.0, "cost_known": True})):
            b["calls"] += 1
            b["in"] += tin
            b["out"] += tout
            b["total"] += tin + tout
            if cost is None:
                b["cost_known"] = False
            else:
                b["cost"] = round(b["cost"] + cost, 6)
    ordered = [buckets[d] for d in sorted(buckets, reverse=True)]
    window = _blank(f"{first}..{today.isoformat()}")
    for d in ordered:
        window["calls"] += d["calls"]
        window["in"] += d["in"]
        window["out"] += d["out"]
        window["total"] += d["total"]
        window["cost"] = round(window["cost"] + d["cost"], 6)
        window["cost_known"] = window["cost_known"] and d["cost_known"]
    return {"today": ordered[0], "days": ordered, "window": window,
            "models": sorted(models.values(), key=lambda m: -m["total"])}
