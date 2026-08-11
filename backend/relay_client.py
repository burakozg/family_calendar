"""Cloud shopping-relay client: mirror the calendar out, drain the phone's
command inbox in. Outbound-only — the NAS never opens an inbound port; the
relay (shopping-relay/) is the one internet-facing piece."""
import asyncio
import hashlib
import json
import os
from datetime import date

import httpx

import ai
import config  # noqa: F401  (loads .env before the env reads below)
from activity_log import log_event
from calendar_store import _add_event, _delete_event
from fsatomic import _atomic_write_text
from recipes import _import_recipe, _import_recipe_photo
from storage import (F_DISPLAY, F_RELAY_APPLIED, F_RELAY_ATTEMPTS, RECIPES_DIR, _sanitize_event,
                     read_events, read_recipe_file, read_recipe_index,
                     read_settings)

# Optional cloud shopping relay: the NAS pushes the week's list up so it can be
# used at the store without exposing the NAS. Outbound-only. See shopping-relay/.
SHOP_RELAY_URL           = os.getenv("SHOP_RELAY_URL", "").rstrip("/")
SHOP_RELAY_PUBLISH_TOKEN = os.getenv("SHOP_RELAY_PUBLISH_TOKEN", "")
SHOP_RELAY_POLL_SECONDS  = int(os.getenv("SHOP_RELAY_POLL_SECONDS", "90"))

# Cost fuse (mailsync's EMAIL_FUSE analogue): at most this many AI-consuming
# inbox commands (recipe_import / recipe_photo — each is a paid model call)
# are executed per drain cycle. The rest stay queued for later cycles, so a
# leaked write token can't burn API credit faster than this rate.
AI_CMD_FUSE   = int(os.getenv("RELAY_AI_CMD_FUSE", "5"))
_AI_CMD_TYPES = ("recipe_import", "recipe_photo")

# Retry budget per command. The fuse above caps AI commands *per cycle*; this caps
# them *across* cycles, which is the leak that actually costs money: a command that
# fails is left unacked to retry, so before this existed one bad recipe re-ran every
# poll forever — ~745 paid extractions a day, until the provider's credit ran out.
MAX_CMD_ATTEMPTS = int(os.getenv("RELAY_CMD_MAX_ATTEMPTS", "3"))


def _relay_ready() -> bool:
    return bool(SHOP_RELAY_URL and SHOP_RELAY_PUBLISH_TOKEN)


# Cloud-relay reachability is logged only on transitions (down↔up) so a persistent
# outage doesn't flood the every-90s sync loop. Per-operation last-known state.
_relay_state: dict = {}

def _log_relay(op: str, ok: bool, detail=None):
    prev = _relay_state.get(op)
    if not ok and prev is not False:
        log_event("cloud", op, "Cloud relay unreachable", level="error", detail=detail)
    elif ok and prev is False:
        log_event("cloud", op, "Cloud relay reachable again", level="info")
    _relay_state[op] = ok


def _relay_headers() -> dict:
    return {"Authorization": f"Bearer {SHOP_RELAY_PUBLISH_TOKEN}"}


def _read_applied() -> list:
    if F_RELAY_APPLIED.exists():
        try:
            return json.loads(F_RELAY_APPLIED.read_text())
        except Exception:
            pass
    return []


def _write_applied(ids: list):
    _atomic_write_text(F_RELAY_APPLIED, json.dumps(ids[-500:], ensure_ascii=False))   # keep it bounded


def _read_attempts() -> dict:
    """{command id: failed attempts}. Persisted, not in-memory: a container
    restart must not hand a poison command a fresh budget."""
    if F_RELAY_ATTEMPTS.exists():
        try:
            d = json.loads(F_RELAY_ATTEMPTS.read_text())
            if isinstance(d, dict):
                return {k: int(v) for k, v in d.items()}
        except Exception:
            pass
    return {}


def _write_attempts(counts: dict):
    trimmed = dict(list(counts.items())[-500:])
    _atomic_write_text(F_RELAY_ATTEMPTS, json.dumps(trimmed, ensure_ascii=False))


def _is_permanent(e: Exception) -> bool:
    """Will the same command fail the same way next cycle?

    A JSONDecodeError (a ValueError) means the model answered and was billed, but
    the answer wasn't parseable JSON. Same input + same model = same reply, so a
    retry buys nothing and costs a full call. Drop it on the first failure.
    Network/provider errors (HTTPException from ai.complete) are the opposite:
    nothing was billed and the next cycle may well succeed."""
    return isinstance(e, ValueError)


def _cmd_summary(ctype: str, payload: dict) -> dict:
    """Enough to identify a dropped command without copying an image into the log."""
    return {"type": ctype, "url": (payload.get("url") or "")[:200],
            "text_chars": len(payload.get("text") or ""),
            "image_b64_chars": len(payload.get("image") or ""),
            "who": str(payload.get("who") or "")}


# The recipe-library signature the relay last confirmed it stores. The full
# mirror (~200 KB) is re-sent only when this differs from the local signature,
# so steady-state publishes stay small; a relay data reset heals on the next
# cycle because its response signature won't match.
_relay_recipes_sig: str | None = None


def _recipes_sig() -> str:
    """Cheap change signature for the recipe library: file names + mtimes.
    Catches edits that don't touch index.json (e.g. steps-only changes)."""
    parts = sorted(f"{p.name}:{p.stat().st_mtime_ns}"
                   for p in RECIPES_DIR.glob("*.json") if p.name != "index.json")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _recipes_mirror(sig: str) -> dict:
    """Index + trimmed records for the phone's Browse tab. Content only —
    photos, provenance (source) and the entry log stay home."""
    records = {}
    for entry in read_recipe_index():
        rid = entry.get("id")
        rec = read_recipe_file(rid) if rid else None
        if rec:
            records[rid] = {k: v for k, v in rec.items()
                            if k not in ("photos", "log", "source")}
    return {"sig": sig, "index": read_recipe_index(), "records": records}


async def publish_calendar() -> bool:
    """Mirror members + upcoming events + meals (and the recipe library when it
    changed) to the relay, so the phone app matches mobile.html. Deliberately
    NOT mirrored: birthdays and recurring items (mobile.html doesn't manage
    them, and birth dates are the most sensitive data in the stores — they
    stay off the internet-facing box). Outbound only; non-fatal."""
    global _relay_recipes_sig
    if not _relay_ready():
        return False
    settings = read_settings()
    today    = date.today().isoformat()
    ev_doc   = read_events()
    events   = [e for e in ev_doc.get("events", [])
                if isinstance(e, dict) and (e.get("endDate") or e.get("date") or "") >= today]
    events.sort(key=lambda e: e.get("date", ""))
    # Meals come free from the display cache (already kept current by build_display_cache).
    dc = json.loads(F_DISPLAY.read_text()) if F_DISPLAY.exists() else {}
    meals = {
        "plan":          dc.get("meal_plan", []),
        "plan_next":     dc.get("meal_plan_next", []),
        "today_meal":    dc.get("today_meal"),
        "tomorrow_meal": dc.get("tomorrow_meal"),
        "today":         dc.get("today", today),
    }
    sig = _recipes_sig()
    payload = {
        "members":     settings.get("members", []),
        "events":      events[:60],
        "eventColors": settings.get("eventColors", {}),
        "meals":       meals,
        "recipesSig":  sig,
    }
    if sig != _relay_recipes_sig:
        payload["recipes"] = _recipes_mirror(sig)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{SHOP_RELAY_URL}/calendar/publish",
                                     headers=_relay_headers(), json=payload)
        ok = resp.status_code == 200
        if ok:
            try:   # what the relay now stores; mismatch → resend next cycle
                _relay_recipes_sig = (resp.json() or {}).get("recipesSig")
            except Exception:
                pass
        _log_relay("relay.publish_calendar", ok, None if ok else {"status": resp.status_code})
        return ok
    except Exception as e:
        _log_relay("relay.publish_calendar", False, {"error": str(e)})
        return False


async def drain_relay_inbox():
    """Pull queued add-commands from the relay, apply them, and ack. Validates and
    sanitizes each; malformed ones are acked and dropped (no poison-pill loops).
    Idempotent via a persisted set of applied ids."""
    if not _relay_ready():
        return
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{SHOP_RELAY_URL}/inbox", headers=_relay_headers())
        except Exception as e:
            _log_relay("relay.inbox", False, {"error": str(e)})
            return
        if resp.status_code != 200:
            _log_relay("relay.inbox", False, {"status": resp.status_code})
            return
        _log_relay("relay.inbox", True)
        items = resp.json().get("items", [])
        if not items:
            return
        applied = _read_applied()
        applied_set = set(applied)
        attempts = _read_attempts()
        attempts_before = dict(attempts)
        handled = []   # ids to ack (applied + malformed + already-seen)
        ai_budget, deferred = AI_CMD_FUSE, 0
        for cmd in items:
            cid = cmd.get("id")
            if not cid:
                continue
            ctype = cmd.get("type")
            if ctype in _AI_CMD_TYPES and cid not in applied_set:
                if ai_budget <= 0:
                    deferred += 1
                    continue   # NOT acked — stays queued for a later cycle
                ai_budget -= 1
            handled.append(cid)
            if cid in applied_set:
                continue   # already applied on a previous cycle; just re-ack
            payload = cmd.get("payload") or {}
            ok      = True   # False = malformed/unknown: still acked+dropped, but logged
            try:
                if ctype == "event":
                    clean = _sanitize_event(payload)
                    ok = clean is not None
                    if clean:
                        await _add_event(clean)
                elif ctype == "recipe_import":
                    await _import_recipe(payload)
                elif ctype == "recipe_photo":
                    await _import_recipe_photo(payload)
                elif ctype == "event_delete":
                    await _delete_event(payload)
                elif ctype == "shopping_extra":
                    # F9: manual extra shopping item added from the phone.
                    from shopping import _apply_extra, publish_shopping
                    from storage import _write_lock
                    week   = (payload.get("week") or "").strip()
                    action = "remove" if payload.get("action") == "remove" else "add"
                    if week:
                        async with _write_lock:
                            ok = _apply_extra(week, payload.get("item", ""),
                                              payload.get("who", ""), action)
                        if ok:
                            await publish_shopping(week)   # reflect back to the phone
                    else:
                        ok = False
                else:
                    # Includes the retired recurring/birthday command types
                    # (phone UI aligned to mobile.html): acked + dropped.
                    ok = False
            except ai.ProviderNotConfigured as e:
                # No API key: nothing was sent and nothing was billed. Retry
                # indefinitely without spending the budget — the fix is an env var,
                # and dropping the family's scanned recipe over it would be worse.
                log_event("cloud", "relay.command",
                          f"Remote {ctype} command waiting on AI config: {e.detail}",
                          level="warn", who=payload.get("who", ""), detail={"id": cid, "type": ctype})
                handled.remove(cid)
                continue
            except Exception as e:
                # Application failed. Retry only if a retry could plausibly differ,
                # and only within a budget — an unbounded retry of a paid call is
                # what drained the API credit before MAX_CMD_ATTEMPTS existed.
                n = attempts.get(cid, 0) + 1
                permanent = _is_permanent(e)
                if permanent or n >= MAX_CMD_ATTEMPTS:
                    why = ("the model's reply could not be parsed, and the same input "
                           "would fail again" if permanent
                           else f"{n} attempts all failed")
                    log_event("cloud", "relay.command",
                              f"Dropped remote {ctype} command — {why}: {e}",
                              level="error", who=payload.get("who", ""),
                              detail={"id": cid, "attempts": n, "permanent": permanent,
                                      "command": _cmd_summary(ctype, payload)})
                    attempts.pop(cid, None)
                    applied.append(cid)          # ack it: stop the relay re-serving it
                    applied_set.add(cid)
                    continue                     # cid stays in `handled`
                attempts[cid] = n
                log_event("cloud", "relay.command",
                          f"Remote {ctype} command failed (attempt {n}/{MAX_CMD_ATTEMPTS}), will retry: {e}",
                          level="error", who=payload.get("who", ""), detail={"id": cid, "type": ctype})
                handled.remove(cid)
                continue
            if ok:
                log_event("cloud", "relay.command", f"Applied remote {ctype} from phone",
                          who=payload.get("who", ""), detail={"type": ctype})
            else:
                log_event("cloud", "relay.command", f"Dropped malformed remote command ({ctype})",
                          level="warn", who=payload.get("who", ""), detail={"id": cid, "type": ctype})
            attempts.pop(cid, None)   # settled: don't carry a stale count forward
            applied.append(cid)
            applied_set.add(cid)
        if deferred:
            log_event("cloud", "relay.fuse",
                      f"Deferred {deferred} AI command(s) to later cycles (fuse={AI_CMD_FUSE})",
                      level="warn", detail={"deferred": deferred, "fuse": AI_CMD_FUSE})
        if attempts != attempts_before:
            _write_attempts(attempts)
        if applied:
            _write_applied(applied)
        if handled:
            try:
                await client.post(f"{SHOP_RELAY_URL}/inbox/ack", headers=_relay_headers(),
                                  json={"ids": handled})
            except Exception:
                pass   # acked next cycle; applied-ids guard prevents duplicates


async def _relay_sync_loop():
    """Background loop: mirror the calendar out and drain the command inbox."""
    while True:
        if _relay_ready():
            try:    await publish_calendar()
            except Exception as e: log_event("cloud", "relay.sync", f"Calendar publish crashed: {e}", level="error")
            try:
                from shopping import republish_shopping   # lazy: shopping imports us
                await republish_shopping()
            except Exception as e: log_event("cloud", "relay.sync", f"Shopping publish crashed: {e}", level="error")
            try:    await drain_relay_inbox()
            except Exception as e: log_event("cloud", "relay.sync", f"Inbox drain crashed: {e}", level="error")
        await asyncio.sleep(max(15, SHOP_RELAY_POLL_SECONDS))
