"""
Family Calendar Backend — app assembly
--------------------------------------
The backend is split into focused modules (see ARCHITECTURE.md):

  config.py         env loading, AI model/key
  fsatomic.py       atomic file writes
  activity_log.py   structured JSONL log + admin trace queries
  storage.py        paths, defaults, write lock, ids, sanitizers, recipe store
  bus.py            SSE fan-out
  display_cache.py  recurrence engine + pre-rendered device payload
  calendar_store.py shared event/birthday/recurring mutation helpers
  relay_client.py   cloud shopping-relay mirror + command-inbox drain
  shopping.py       shopping list, AI aisle tagging, relay publish (routes)
  meals.py          weekly plan + two-step AI planner (routes)
  recipes.py        recipe CRUD + AI extraction/import pipelines (routes)
  mailsync.py       mailbox.org mail/calendar sync (MAILSYNC_DESIGN.md)
  willys.py         Willys price lookup (read-only, anonymous) for list costing

main.py keeps: app setup + middleware (Host allowlist, body cap, X-Who), the
unhandled-exception logger, settings/events routes, logs/display/SSE routes,
the backup loop, startup, and static mounts.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Deps: pip install -r requirements.txt
"""
import asyncio
import ipaddress
import io
import json
import os
import traceback
import zipfile
from datetime import date
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config  # noqa: F401  (loads .env first)
import mailsync
import meals
import recipes
import shopping
from activity_log import (LOG_CATEGORIES, LOG_FILE, LOG_LEVELS, _current_who,
                          log_event, parse_ai_json, read_logs)
from bus import _subscribers, broadcast
from calendar_store import (_add_event, _add_recurring, _delete_by_id,
                            _delete_event, _delete_recurring)
from config import AI_MODEL, ANTHROPIC_API_KEY
from display_cache import _cache_warned, _recurring_occurrences, build_display_cache, fold_ascii
from fsatomic import _atomic_write_text
from recipes import _host_is_public
from relay_client import SHOP_RELAY_POLL_SECONDS, _relay_ready, _relay_sync_loop
from storage import (ALLOWED_ICONS, DATA, DEFAULTS, F_EVENTS, F_MEALS,
                     F_RELAY_APPLIED, F_SETTINGS, F_SHOPPING, PHOTOS_DIR,
                     RECIPE_PENDING_DIR, RECIPES_DIR, _ensure_ids, _new_id,
                     _sanitize_event, _sanitize_recurring, _write_lock, read,
                     read_events, read_meals, read_settings, rebuild_recipe_index,
                     recipe_index_needs_rebuild, write)

app = FastAPI(title="Family Calendar API")
# No CORS middleware on purpose: every frontend is served by this same backend
# (same origin), and the Inky Frame is a raw HTTP client that CORS doesn't
# apply to. With no auth, wide-open CORS would let any web page opened on a
# LAN device silently call the mutation routes cross-origin.

# Request body cap. Generous because photo/zip uploads arrive base64-inside-JSON
# (40 MB zip ≈ 54 MB base64); everything else is far below it. Uvicorn itself
# imposes no limit, so without this one huge POST could exhaust container memory.
MAX_BODY = int(os.getenv("MAX_BODY", str(64 * 1024 * 1024)))

# Host-header allowlist — the DNS-rebinding guard. A rebinding attack makes a
# victim browser send requests whose Host is the *attacker's domain* (resolving
# to this LAN IP), so: IP-literal Hosts are always allowed (direct LAN access,
# the Inky), while hostnames must be allowlisted. Extend via ALLOWED_HOSTS
# (comma-separated hostnames, no port) e.g. when a reverse proxy is added.
ALLOWED_HOSTS = {h.strip().lower()
                 for h in os.getenv("ALLOWED_HOSTS", "localhost").split(",") if h.strip()}

# When set (the relay app's origin, e.g. https://family-shopping-relay.fly.dev),
# the relay app's "AI content" tab may embed this backend in an iframe; every
# response then carries a frame-ancestors CSP restricted to 'self' + that origin.
# Unset = no CSP header (LAN-only posture, unchanged behavior).
EMBED_ORIGIN = os.getenv("EMBED_ORIGIN", "").strip().rstrip("/")

def _host_allowed(header: str) -> bool:
    h = (header or "").strip().lower()
    if h.startswith("["):                      # [ipv6]:port
        h = h[1:h.index("]")] if "]" in h else h[1:]
    else:
        h = h.split(":")[0]
    if not h:
        return False
    if h in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False

@app.middleware("http")
async def _capture_initiator(request: Request, call_next):
    """Record who initiated the request (frontends send X-Who) for the log's 'who',
    enforce the Host allowlist, and reject oversized request bodies early."""
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse({"detail": "Misdirected request"}, status_code=421)
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_BODY:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    _current_who.set(request.headers.get("x-who", ""))
    resp = await call_next(request)
    if EMBED_ORIGIN:
        resp.headers.setdefault("Content-Security-Policy",
                                f"frame-ancestors 'self' {EMBED_ORIGIN}")
    return resp

@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception):
    """Record unhandled exceptions in the activity log before they become a bare
    500. Without this an app bug is invisible to the admin Logs UI — the frontend
    only ever shows its own generic toast, and the traceback lives in `docker logs`
    where nobody looks. (A single malformed recipe once 500'd every meal-planner
    call for days with no log line at all.)

    Starlette re-raises after this returns, so uvicorn still prints the traceback;
    the response body stays generic on purpose — internals aren't for the client."""
    log_event("system", "unhandled", f"{request.method} {request.url.path} failed: "
                                     f"{type(exc).__name__}: {exc}",
              level="error",
              detail={"path": request.url.path, "method": request.method,
                      "type": type(exc).__name__,
                      "traceback": traceback.format_exc()[-4000:]})
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

@app.get("/healthz")
def healthz():
    """Liveness probe — also used by the relay app's AI content tab to detect
    the home network (a no-cors fetch: reachability is the only signal needed)."""
    return {"ok": True}

# ── Routes — AI model picker ──────────────────────────────────────────────────
@app.get("/ai/models")
def get_ai_models():
    """Selectable AI models (name, relative cost tier, whether they accept images,
    and a per-role `recVision`/`recText` on the ones recommended for this app's
    tasks), the model selected for each role, and which providers have an API key
    configured — drives the admin pickers."""
    import ai
    return {"models": ai.AI_MODELS, "selected": ai.selected_models(),
            "providers": ai.providers_status()}

@app.get("/ai/usage")
def get_ai_usage(days: int = 14):
    """Token consumption per day (and per model) over the last `days` — drives
    the admin AI panel's usage readout."""
    import ai_usage
    return ai_usage.summary(days)

# ── Routes — Settings ─────────────────────────────────────────────────────────
@app.get("/settings")
def get_settings(): return read_settings()

@app.post("/settings")
async def post_settings(request: Request):
    body = await request.json()
    async with _write_lock:
        write(F_SETTINGS, body)
        build_display_cache()
    await broadcast("update", {"section": "settings"})
    log_event("data", "settings.save", "Settings saved",
              detail={"members": len(body.get("members", [])),
                      "familyName": body.get("familyName", ""),
                      "aiModel": (body.get("ai") or {}).get("model")})
    return {"ok": True}

# ── Routes — Events ───────────────────────────────────────────────────────────
@app.get("/events")
def get_events(): return read_events()

@app.post("/events")
async def post_events(request: Request):
    body = await request.json()
    async with _write_lock:
        _ensure_ids(body)   # newly added items from the admin UI arrive id-less
        write(F_EVENTS, body)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", "events.replace",
              f"Events document replaced ({len(body.get('events', []))} events, "
              f"{len(body.get('birthdays', []))} birthdays, {len(body.get('recurring', []))} recurring)")
    return {"ok": True}

@app.post("/events/add")
async def add_event(request: Request):
    body = await request.json()   # {date, who, icon, label}
    await _add_event(body)
    log_event("data", "event.add",
              f"Added event '{str(body.get('label', ''))[:80]}' on {body.get('date', '')}",
              detail={"who": body.get("who", "")})
    return {"ok": True}

@app.delete("/events/{idx}")
async def delete_event(idx: int):
    async with _write_lock:
        ev = read_events()
        gone = ev["events"].pop(idx)
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", "event.delete",
              f"Deleted event '{gone.get('label', '')}' on {gone.get('date', '')}")
    return {"ok": True}

@app.post("/birthdays/add")
async def add_birthday(request: Request):
    body = await request.json()   # {name, month, day, year?}
    try:
        y = int(body.get("year"))
        if 1900 <= y <= 2100:
            body["year"] = y
        else:
            body.pop("year", None)
    except (TypeError, ValueError):
        body.pop("year", None)
    async with _write_lock:
        if not body.get("id"):
            body["id"] = _new_id()
        ev = read_events()
        ev.setdefault("birthdays", []).append(body)
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", "birthday.add",
              f"Added birthday '{str(body.get('name', ''))[:80]}' ({body.get('month')}/{body.get('day')})")
    return {"ok": True}

@app.delete("/birthdays/{idx}")
async def delete_birthday(idx: int):
    async with _write_lock:
        ev = read_events()
        gone = ev["birthdays"].pop(idx)
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", "birthday.delete",
              f"Deleted birthday '{gone.get('name', '')}' ({gone.get('month')}/{gone.get('day')})")
    return {"ok": True}

@app.post("/recurring/add")
async def add_recurring(request: Request):
    body = await request.json()
    await _add_recurring(body)
    log_event("data", "recurring.add",
              f"Added recurring '{str(body.get('label', ''))[:80]}' every {body.get('step')}d from {body.get('startDate', '')}")
    return {"ok": True}

@app.delete("/recurring/{idx}")
async def delete_recurring(idx: int):
    async with _write_lock:
        ev = read_events()
        gone = ev["recurring"].pop(idx)
        write(F_EVENTS, ev)
        build_display_cache()
    await broadcast("update", {"section": "events"})
    log_event("data", "recurring.delete",
              f"Deleted recurring '{gone.get('label', '')}' (every {gone.get('step')}d)")
    return {"ok": True}

# Delete by stable id (A7) — race-free alternative to the index routes.
@app.delete("/events/by-id/{item_id}")
async def delete_event_by_id(item_id: str):
    return await _delete_by_id("events", item_id)

@app.delete("/birthdays/by-id/{item_id}")
async def delete_birthday_by_id(item_id: str):
    return await _delete_by_id("birthdays", item_id)

@app.delete("/recurring/by-id/{item_id}")
async def delete_recurring_by_id(item_id: str):
    return await _delete_by_id("recurring", item_id)

# ── Domain routers ────────────────────────────────────────────────────────────
app.include_router(meals.router)
app.include_router(shopping.router)
app.include_router(recipes.router)

# ── Routes — Logs ─────────────────────────────────────────────────────────────
@app.get("/logs")
def get_logs(category: str = "", level: str = "", who: str = "", q: str = "",
             limit: int = 200, since: str = ""):
    """Filtered activity log for the admin trace UI (newest first)."""
    return {"entries": read_logs(category or None, level or None, who or None, q or None,
                                 min(max(limit, 1), 1000), since or None)}

@app.get("/logs/meta")
def get_logs_meta():
    """Filter options for the log UI: fixed categories/levels plus the whos seen."""
    whos = set()
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:    whos.add(json.loads(line).get("who", ""))
            except Exception: pass
    return {"categories": LOG_CATEGORIES, "levels": LOG_LEVELS, "whos": sorted(w for w in whos if w)}

@app.delete("/logs")
def clear_logs():
    if LOG_FILE.exists():
        LOG_FILE.unlink()
    log_event("system", "logs.clear", "Activity log cleared")
    return {"ok": True}

# ── Routes — Display (Pico) ───────────────────────────────────────────────────
from storage import F_DISPLAY  # noqa: E402  (grouped with its only consumers)

@app.get("/display-data")
async def get_display_data(week_offset: int = 0, month_offset: int | None = None,
                           ascii: int = 0):
    """Serve the display payload. Non-zero offsets are computed on demand and never
    cached. The cache anchored on today is reused only while it's still for today — on
    a day rollover it's rebuilt, which is also what re-anchors the rolling 4-week
    window. async (not sync-in-threadpool) so the rebuild can hold the write lock.

    `month_offset` is the pre-rolling-window parameter, kept because device firmware
    drifts from this repo: a backend deployed ahead of a reflash would otherwise hand
    an old Inky an offset it silently ignores. One month ≈ 4 weeks.

    `ascii=1` folds the text to ASCII for the Inky Frame, whose bitmap8 font has no
    glyphs above 126 (Turkish, Swedish). Applied at serve time, never stored: the
    cache, the web UI and the phone keep proper Unicode."""
    if month_offset is not None:
        if "legacy_month_offset" not in _cache_warned:
            _cache_warned.add("legacy_month_offset")
            log_event("system", "display.legacy_param",
                      "A client asked for month_offset; serving the rolling window "
                      "4 weeks per month. Reflash the Inky to send week_offset.",
                      level="warn")
        week_offset = week_offset or month_offset * 4
    if week_offset != 0:
        payload = build_display_cache(week_offset)    # computed on demand, never written
    else:
        payload = None
        if F_DISPLAY.exists():
            cached = json.loads(F_DISPLAY.read_text())
            if cached.get("today") == str(date.today()):
                payload = cached
        if payload is None:
            async with _write_lock:
                payload = build_display_cache(0)
    return fold_ascii(payload) if ascii else payload

@app.post("/display-data/rebuild")
async def rebuild_display():
    """Force rebuild the display cache."""
    async with _write_lock:
        payload = build_display_cache()
    await broadcast("update", {"section": "display"})
    log_event("system", "display.rebuild", "Display cache force-rebuilt")
    return {"ok": True, "cells": len(payload["cells"])}

# ── SSE ───────────────────────────────────────────────────────────────────────
@app.get("/stream")
async def stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _subscribers.append(queue)
    async def generator() -> AsyncGenerator[str, None]:
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                if await request.is_disconnected(): break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in _subscribers: _subscribers.remove(queue)
    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Daily backup ──────────────────────────────────────────────────────────────
# The flat JSONs are the only copy of the family's data. Once a day, zip the
# stores + recipe records into data/backups/ and keep the last BACKUP_KEEP.
# Photos, cache/, logs.jsonl, and backups/ itself are deliberately excluded
# (bulky and reproducible/expendable). NAS-level snapshots are the second layer.
BACKUPS     = DATA / "backups"
BACKUP_KEEP = 14

def _write_data_archive(z: zipfile.ZipFile) -> None:
    """Add the canonical JSON stores + recipe records (incl. the recipe index)
    to an open zip. Shared by the daily backup and the /export download. Photos,
    cache/, and logs are excluded — bulky and reproducible/expendable."""
    for f in (F_SETTINGS, F_EVENTS, F_MEALS, F_SHOPPING, F_RELAY_APPLIED):
        if f.exists():
            z.write(f, f.name)
    for rf in sorted(RECIPES_DIR.glob("*.json")):
        z.write(rf, f"recipes/{rf.name}")
    for pf in sorted(RECIPE_PENDING_DIR.glob("*.json")):
        z.write(pf, f"recipes/pending/{pf.name}")

def _write_recipes_archive(z: zipfile.ZipFile) -> None:
    """Add only the recipe records + index + photos — a portable, shareable
    recipe bundle (photos included, unlike the space-conscious full backup)."""
    for rf in sorted(RECIPES_DIR.glob("*.json")):
        z.write(rf, f"recipes/{rf.name}")
    for pf in sorted(RECIPE_PENDING_DIR.glob("*.json")):
        z.write(pf, f"recipes/pending/{pf.name}")
    if PHOTOS_DIR.exists():
        for pf in sorted(PHOTOS_DIR.glob("*")):
            if pf.is_file():
                z.write(pf, f"recipes/photos/{pf.name}")

def make_backup() -> Path | None:
    """Write data/backups/YYYY-MM-DD.zip (atomic); None if today's already exists."""
    BACKUPS.mkdir(exist_ok=True)
    target = BACKUPS / f"{date.today().isoformat()}.zip"
    if target.exists():
        return None
    tmp = target.with_name(target.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        _write_data_archive(z)
    os.replace(tmp, target)
    for old in sorted(BACKUPS.glob("*.zip"))[:-BACKUP_KEEP]:
        old.unlink()
    return target

# ── Data export (F14) ─────────────────────────────────────────────────────────
# On-demand download of the same content the daily backup captures. Restore is
# manual and deliberately simple: unzip into the data/ directory (recipes land
# in data/recipes/). No live import endpoint — avoids a one-click way to clobber
# the family's only copy of its data.
def _zip_response(builder, filename: str) -> Response:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        builder(z)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/export")
async def export_data():
    """Download a zip of all JSON stores + recipes (restore: unzip into data/)."""
    stamp = date.today().isoformat()
    async with _write_lock:                       # consistent cross-file snapshot
        resp = _zip_response(_write_data_archive, f"calendar-{stamp}.zip")
    log_event("system", "export", "Full data export downloaded")
    return resp

@app.get("/export/recipes")
async def export_recipes():
    """Download a zip of just the recipes (records + index + photos)."""
    stamp = date.today().isoformat()
    async with _write_lock:
        resp = _zip_response(_write_recipes_archive, f"recipes-{stamp}.zip")
    log_event("system", "export", "Recipes export downloaded")
    return resp

async def _backup_loop():
    """Hourly tick; makes at most one backup per day. Failures log, never crash."""
    while True:
        try:
            async with _write_lock:   # consistent cross-file snapshot; zipping is sub-second
                made = await asyncio.to_thread(make_backup)
            if made:
                log_event("system", "backup", f"Backup written: {made.name}",
                          detail={"bytes": made.stat().st_size, "keep": BACKUP_KEEP})
        except Exception as e:
            log_event("system", "backup", f"Backup failed: {e}", level="error")
        await asyncio.sleep(3600)

# ── Mailbox.org mail/calendar sync (backend/mailsync.py; MAILSYNC_DESIGN.md) ──
@app.get("/mailsync/status")
def mailsync_status():
    """Sync status for the admin card. Never includes credentials."""
    return mailsync.status()

@app.post("/mailsync/run")
async def mailsync_run():
    """Run one sync cycle immediately (guarded against overlapping the loop)."""
    if not mailsync.configured_env():
        raise HTTPException(400, f"Mail sync not configured — set {', '.join(mailsync.missing_env())}")
    await mailsync.run_cycle()
    return mailsync.status()

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # Recipe index schema bump (F3): rebuild once when the rows predate the new
    # search/filter fields (course, dietary, times, ingredients, source_value).
    if recipe_index_needs_rebuild():
        async with _write_lock:
            n = rebuild_recipe_index()
        log_event("data", "recipe.reindex", f"Rebuilt recipe index on startup ({n} recipes)")
    build_display_cache()
    print("Display cache built on startup")
    import ai
    log_event("system", "server.start", "Backend started",
              detail={"models": ai.selected_models(), "relay": _relay_ready(),
                      "relay_poll_s": SHOP_RELAY_POLL_SECONDS})
    asyncio.create_task(_backup_loop())
    if _relay_ready():
        asyncio.create_task(_relay_sync_loop())
        print(f"Relay sync loop started (every {SHOP_RELAY_POLL_SECONDS}s)")
    if mailsync.configured_env():
        # Started even when the settings toggle is off — the loop checks the
        # toggle every cycle, so enabling in the admin UI needs no restart.
        asyncio.create_task(mailsync.mailsync_loop())
        print(f"Mailsync loop started (every {mailsync.MAILSYNC_POLL_SECONDS}s)")

# ── Static files ──────────────────────────────────────────────────────────────
# Recipe photos — mounted before the catch-all frontend mount below.
app.mount("/recipe-photos", StaticFiles(directory=str(PHOTOS_DIR)), name="recipe-photos")

frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
