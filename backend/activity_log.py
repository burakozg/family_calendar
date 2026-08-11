"""Activity log — one structured JSON object per line in data/logs.jsonl.
Used by the admin "Logs" trace UI to follow activity and diagnose failures,
especially unexpected AI responses."""
import contextvars
import json
import os
from datetime import datetime
from pathlib import Path

import config  # noqa: F401  (loads .env before the DATA_DIR read below)
from fsatomic import _atomic_write_text

LOG_FILE       = Path(os.getenv("DATA_DIR", "/data")) / "logs.jsonl"
LOG_MAX        = 3000                                   # keep the most recent N entries
LOG_CATEGORIES = ["ai", "import", "connectivity", "cloud", "data", "system", "mailsync"]
LOG_LEVELS     = ["info", "warn", "error"]
_current_who   = contextvars.ContextVar("who", default="")   # set per request from X-Who


def _trim_for_log(detail):
    """Keep a log entry's detail bounded and JSON-serialisable."""
    try:
        s = json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:
        return {"repr": str(detail)[:8000]}
    return detail if len(s) <= 8000 else {"_truncated": True, "preview": s[:8000]}


def log_event(category, action, message, level="info", who="", detail=None):
    """Append one structured entry to the activity log. Never raises."""
    entry = {
        "ts":       datetime.now().isoformat(timespec="seconds"),
        "level":    level if level in LOG_LEVELS else "info",
        "category": category,
        "action":   action,
        "who":      who or _current_who.get() or "",
        "message":  str(message)[:1000],
    }
    if detail is not None:
        entry["detail"] = _trim_for_log(detail)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if LOG_FILE.stat().st_size > 2_000_000:            # trim occasionally
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-LOG_MAX:]
            _atomic_write_text(LOG_FILE, "\n".join(lines) + "\n")
    except Exception:
        pass
    return entry


def read_logs(category=None, level=None, who=None, q=None, limit=200, since=None):
    """Return the newest matching log entries (newest first). `since` is an ISO
    date (YYYY-MM-DD); entries from that day onward match (lexical compare works
    because ts is ISO)."""
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:    e = json.loads(line)
        except Exception: continue
        if category and e.get("category") != category: continue
        if level    and e.get("level")    != level:    continue
        if who      and e.get("who")       != who:      continue
        if since    and e.get("ts", "")    < since:     continue
        if q:
            hay = f"{e.get('message','')} {e.get('action','')} {json.dumps(e.get('detail') or '', ensure_ascii=False)}".lower()
            if q.lower() not in hay: continue
        out.append(e)
    out.reverse()
    return out[:limit]


def parse_ai_json(raw, action):
    """Parse an AI JSON reply; on failure log the raw response and re-raise.
    This is where unexpected AI responses are captured for tracing."""
    clean = (raw or "").replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception as e:
        log_event("ai", action, f"Unexpected AI response — not valid JSON ({e})",
                  level="error", detail={"raw": (raw or "")[:4000]})
        raise
