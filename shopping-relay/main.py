"""Shopping / calendar relay — a tiny internet-facing bridge to the family NAS.

The Family Calendar backend lives on a home NAS that is not exposed to the
internet. This relay is the one small piece that *is* internet-facing:

- Shopping (read-mostly): the NAS pushes the week's list up here (outbound only)
  and the phone reads/ticks it at the store.
- Calendar (reverse channel): the phone queues "add/delete event" and "import
  recipe" commands into an inbox; the NAS drains it on an outbound poll and
  applies them. The NAS also mirrors members + upcoming events + meals + the
  recipe library here, so the phone app matches the home mobile.html.

Auth uses long random bearer tokens with least-privilege scopes:
  - PUBLISH_TOKEN     — the NAS (publish list, mirror calendar, drain inbox).
  - DEVICE_TOKEN      — phones, read/shop scope (read list, tick items, read cal).
  - DEVICE_WRITE_TOKEN — phones authorized to add calendar entries (POST /inbox).
                         Falls back to DEVICE_TOKEN when unset (single-token mode).

Deliberately mirrors the main backend's ethos: FastAPI + a single flat JSON file,
no database. The file should live on a persistent volume.
"""
import hmac, json, os, sys, time, uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse

PUBLISH_TOKEN      = os.getenv("PUBLISH_TOKEN", "")
DEVICE_TOKEN       = os.getenv("DEVICE_TOKEN", "")
# Separate write scope so a leaked read/shop token can't inject calendar entries.
DEVICE_WRITE_TOKEN = os.getenv("DEVICE_WRITE_TOKEN", "") or DEVICE_TOKEN
RELAY_DATA         = Path(os.getenv("RELAY_DATA", "/data/relay.json"))
HTML_FILE          = Path(__file__).with_name("shop.html")
INBOX_MAX          = 100                                    # cap pending commands
# 1 MB: the NAS's recipe-library mirror (POST /calendar/publish) is ~200 KB
# today and grows with the library; everything else is tiny.
MAX_BODY           = int(os.getenv("MAX_BODY", str(1024 * 1024)))
RATE_LIMIT         = int(os.getenv("RATE_LIMIT", "120"))    # requests/window per IP
RATE_WINDOW        = int(os.getenv("RATE_WINDOW", "60"))    # seconds

# The home backend's HTTPS URL (reverse proxy with a valid cert in front of the
# NAS, e.g. https://your-name.duckdns.org) — powers the app's "AI content" tab:
# reachability probe + embedded mobile.html when the phone is on the home
# network (AI meal planning + reviewed photo scanning). Optional.
HOME_APP_URL  = os.getenv("HOME_APP_URL", "").strip().rstrip("/")
_hp           = urlparse(HOME_APP_URL) if HOME_APP_URL else None
_HOME_ORIGIN  = f"{_hp.scheme}://{_hp.netloc}" if _hp and _hp.scheme and _hp.netloc else ""

# Aligned to mobile.html: one-off events + recipe imports only (recurring and
# birthdays are managed in the home admin, and stay off this box entirely).
# recipe_photo carries a downscaled base64 photo (~0.5 MB) — see MAX_BODY.
INBOX_TYPES = ("event", "recipe_import", "recipe_photo", "event_delete", "shopping_extra")


def rlog(event: str, **kw):
    """One structured line to stdout (docker/Fly logs). Never token values."""
    try:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "event": event, **kw}, ensure_ascii=False), file=sys.stdout, flush=True)
    except Exception:
        pass

RELAY_DATA.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Family Relay")

# ── Abuse controls + security headers (single middleware) ─────────────────────
_hits: dict = {}   # ip -> deque[timestamps]  (in-memory sliding-window rate limit)

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")

_CSP = ("default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self' data:; "
        + (f"connect-src 'self' {_HOME_ORIGIN}; " if _HOME_ORIGIN else "connect-src 'self'; ")
        + (f"frame-src {_HOME_ORIGIN}; " if _HOME_ORIGIN else "frame-src 'none'; ")
        + "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com")
_SEC_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

@app.middleware("http")
async def guard(request: Request, call_next):
    # 1. Reject oversized bodies early (defends memory/storage abuse).
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_BODY:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    # 2. Per-IP sliding-window rate limit (health checks exempt).
    if request.url.path == "/healthz":
        resp = await call_next(request)
        for k, v in _SEC_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp
    ip  = _client_ip(request)
    now = time.time()
    dq  = _hits.setdefault(ip, deque())
    while dq and dq[0] < now - RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        rlog("rate_limited", ip=ip, path=request.url.path)
        return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": str(RATE_WINDOW), **_SEC_HEADERS})
    dq.append(now)
    if len(_hits) > 10000:   # bound memory: drop the emptiest buckets
        for k in [k for k, v in list(_hits.items()) if not v][:5000]:
            _hits.pop(k, None)
    # 3. Handle + attach security headers to every response.
    resp = await call_next(request)
    for k, v in _SEC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _blank_store() -> dict:
    return {
        "shopping": {"week": "", "start": "", "published_at": "", "days": [], "extras": [], "have": [], "bought": [], "state_updated_at": ""},
        "calendar": {"members": [], "events": [], "eventColors": {}, "meals": {}, "recipesSig": "", "published_at": ""},
        "recipes":  {"sig": "", "index": [], "records": {}, "published_at": ""},
        "inbox": [],
    }

def read_store() -> dict:
    store = _blank_store()
    if RELAY_DATA.exists():
        try:
            doc = json.loads(RELAY_DATA.read_text())
        except Exception:
            doc = {}
        if isinstance(doc, dict):
            # Migrate the old flat shopping-only doc (had top-level "week").
            if "shopping" not in doc and "week" in doc:
                store["shopping"].update(doc)
            else:
                for k in store:
                    if k in doc:
                        store[k] = doc[k]
    return store

def write_store(store: dict):
    # Temp file + atomic rename: a crash mid-write can't leave a torn store.
    tmp = RELAY_DATA.with_name(RELAY_DATA.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(store, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, RELAY_DATA)

def _bearer(request: Request) -> str:
    # Header only. Phone provisioning uses the URL *fragment* (#t=…), which
    # never reaches the server — so accepting ?t= here would only create a
    # token-in-access-logs surface with no user.
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

def require(request: Request, *allowed: str):
    """Constant-time check that the request carries one of the allowed tokens."""
    tok = _bearer(request)
    ok = any(a and hmac.compare_digest(tok, a) for a in allowed)
    if not ok:
        rlog("auth_reject", ip=_client_ip(request), path=request.url.path)
        raise HTTPException(401, "Unauthorized")


@app.get("/healthz")
def healthz():
    return {"ok": True}

# The shell (/, /app.js, /sw.js) is served must-revalidate. Without a Cache-Control
# header browsers fall back to heuristic caching, and a phone can keep serving a
# stale shell long after a deploy — which is exactly what happened. The ETag still
# makes the revalidation a cheap 304.
_NO_CACHE = {"Cache-Control": "no-cache"}

@app.get("/")
def root():
    return FileResponse(HTML_FILE, headers=_NO_CACHE)

# ── PWA assets (public, no auth) ──────────────────────────────────────────────
HERE = Path(__file__).parent
_PWA_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js":                ("sw.js",                "application/javascript"),
    "/icon-192.png":         ("icon-192.png",         "image/png"),
    "/icon-512.png":         ("icon-512.png",         "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}

@app.get("/app.js")
def app_js():
    return FileResponse(HERE / "app.js", media_type="application/javascript",
                        headers=_NO_CACHE)

@app.get("/favicon.ico")
def favicon():
    return FileResponse(HERE / "icon-192.png", media_type="image/png")

@app.get("/manifest.webmanifest")
def manifest():
    f, mt = _PWA_FILES["/manifest.webmanifest"]
    return FileResponse(HERE / f, media_type=mt)

@app.get("/sw.js")
def service_worker():
    f, mt = _PWA_FILES["/sw.js"]
    # Allow the SW to control the whole origin even though it's served from /sw.js.
    return FileResponse(HERE / f, media_type=mt,
                        headers={"Service-Worker-Allowed": "/", **_NO_CACHE})

@app.get("/icon-192.png")
def icon_192():
    return FileResponse(HERE / "icon-192.png", media_type="image/png")

@app.get("/icon-512.png")
def icon_512():
    return FileResponse(HERE / "icon-512.png", media_type="image/png")

@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return FileResponse(HERE / "apple-touch-icon.png", media_type="image/png")

# ── Shopping (NAS → phone) ────────────────────────────────────────────────────
@app.post("/publish")
async def publish(request: Request):
    """NAS pushes the rolling shopping window here. Body: {week, start, days, have, extras}.
    The window slides day by day, so 'bought' ticks are carried forward and only
    pruned when their item is no longer in the list (a recipe day rolled off, or an
    extra was removed) — never reset by the calendar week turning over."""
    require(request, PUBLISH_TOKEN)
    body = await request.json()
    week  = (body.get("week") or "").strip()
    start = (body.get("start") or "").strip()
    days = body.get("days") or []
    have = list(body.get("have") or [])
    extras = list(body.get("extras") or [])

    store = read_store()
    prev  = store["shopping"]
    bought = list(prev.get("bought") or [])
    if bought:
        # Keep ticks whose item still appears — in a recipe OR in a manual extra (F9).
        live = {(ing.get("item") or "").strip().lower()
                for d in days for ing in (d.get("ingredients") or [])}
        live |= {(e.get("item") or "").strip().lower() for e in extras if isinstance(e, dict)}
        bought = [b for b in bought if b in live]

    store["shopping"] = {
        "week": week, "start": start, "days": days, "extras": extras, "have": have, "bought": bought,
        "published_at": _now(), "state_updated_at": prev.get("state_updated_at", ""),
    }
    write_store(store)
    rlog("publish_shopping", week=week, start=start, days=len(days))
    return {"ok": True}

@app.get("/list")
def get_list(request: Request):
    """Phone (or NAS pull-back) reads the current published list + check-off state."""
    require(request, DEVICE_TOKEN, PUBLISH_TOKEN)
    return read_store()["shopping"]

@app.post("/state")
async def set_state(request: Request):
    """Phone updates check-off state. Body: {have, bought}."""
    require(request, DEVICE_TOKEN)
    body = await request.json()
    store = read_store()
    store["shopping"]["have"] = list(body.get("have") or [])
    store["shopping"]["bought"] = list(body.get("bought") or [])
    store["shopping"]["state_updated_at"] = _now()
    write_store(store)
    return {"ok": True}

# ── Calendar mirror (NAS → phone) ─────────────────────────────────────────────
@app.post("/calendar/publish")
async def calendar_publish(request: Request):
    """NAS mirrors members + upcoming events + meals; the recipe-library blob
    rides along only when it changed (the NAS compares our returned signature).
    Body: {members, events, eventColors, meals, recipesSig, recipes?}."""
    require(request, PUBLISH_TOKEN)
    body = await request.json()
    store = read_store()
    rec = body.get("recipes")
    if isinstance(rec, dict) and isinstance(rec.get("index"), list):
        store["recipes"] = {"sig": str(rec.get("sig") or ""), "index": rec["index"],
                            "records": rec.get("records") or {}, "published_at": _now()}
    store["calendar"] = {
        "members":     body.get("members") or [],
        "events":      body.get("events") or [],
        "eventColors": body.get("eventColors") or {},
        "meals":       body.get("meals") or {},   # {plan, plan_next, today_meal, tomorrow_meal, week}
        "recipesSig":  store["recipes"].get("sig", ""),  # phone refetches /recipes on change
        "published_at": _now(),
    }
    write_store(store)
    return {"ok": True, "recipesSig": store["recipes"].get("sig", "")}

@app.get("/calendar")
def calendar_get(request: Request):
    """Phone reads the mirrored members, upcoming events, and the week's meals."""
    require(request, DEVICE_TOKEN, PUBLISH_TOKEN)
    return read_store()["calendar"]

@app.get("/recipes")
def recipes_get(request: Request):
    """Phone reads the mirrored recipe library for the Browse tab (fetched only
    when /calendar's recipesSig differs from its cached copy)."""
    require(request, DEVICE_TOKEN, PUBLISH_TOKEN)
    return read_store()["recipes"]

@app.get("/app-config")
def app_config(request: Request):
    """Phone-app config (token-gated): the home backend URL for the AI content tab."""
    require(request, DEVICE_TOKEN, PUBLISH_TOKEN)
    return {"homeUrl": HOME_APP_URL}

# ── Command inbox (phone → NAS) ───────────────────────────────────────────────
@app.post("/inbox")
async def inbox_add(request: Request):
    """Phone queues a command. Body: {type: 'event'|'event_delete'|'recipe_import'|
    'recipe_photo'|'shopping_extra', payload}.
    Requires the WRITE scope (not the read/shop token). Returns the assigned id."""
    require(request, DEVICE_WRITE_TOKEN)
    body = await request.json()
    ctype = body.get("type")
    if ctype not in INBOX_TYPES:
        raise HTTPException(400, "Unknown command type")
    if not isinstance(body.get("payload"), dict):
        raise HTTPException(400, "Missing payload")
    store = read_store()
    if len(store["inbox"]) >= INBOX_MAX:
        rlog("inbox_full", type=ctype)
        raise HTTPException(429, "Inbox full — try again once the home server catches up")
    cmd = {"id": uuid.uuid4().hex, "type": ctype, "payload": body["payload"], "created_at": _now()}
    store["inbox"].append(cmd)
    write_store(store)
    rlog("inbox_add", type=ctype)
    return {"ok": True, "id": cmd["id"]}

@app.get("/inbox")
def inbox_list(request: Request):
    """NAS pulls pending commands to apply."""
    require(request, PUBLISH_TOKEN)
    return {"items": read_store()["inbox"]}

@app.post("/inbox/ack")
async def inbox_ack(request: Request):
    """NAS confirms which commands it has applied; the relay drops them."""
    require(request, PUBLISH_TOKEN)
    body = await request.json()
    ids  = set(body.get("ids") or [])
    store = read_store()
    store["inbox"] = [c for c in store["inbox"] if c.get("id") not in ids]
    write_store(store)
    return {"ok": True, "remaining": len(store["inbox"])}
