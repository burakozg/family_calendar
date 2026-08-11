"""Recipe library routes + AI extraction/import pipelines: photo extraction,
URL/text extraction (SSRF-guarded fetch), one-shot link import, Notion bulk
import, AI generation, and unit normalization."""
import asyncio
import base64
import difflib
import io
import ipaddress
import json
import os
import posixpath
import re
import socket
import unicodedata
import urllib.parse
import uuid
import zipfile
from datetime import date, datetime
from html import unescape
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

import ai
from activity_log import log_event, parse_ai_json
from bus import broadcast
from display_cache import build_display_cache
from storage import (PENDING_DIR, RECIPE_COURSES, SHOP_CATEGORIES, _attach_photo_bytes,
                     _attach_pending_photo, _save_ai_recipe, _slugify, _unique_recipe_id,
                     _upsert_index, _write_lock, delete_pending_recipe, delete_recipe_file,
                     list_pending_recipes, read_pending_recipe, read_recipe_file,
                     read_recipe_index, rebuild_recipe_index, write_pending_recipe,
                     write_recipe_file, write_recipe_index)

_COURSES = set(RECIPE_COURSES)

def _valid_course(val) -> str:
    """A recognized course (lowercased), or '' for missing/invalid (→ 'other' in UIs)."""
    c = str(val or "").strip().lower()
    return c if c in _COURSES else ""

def _coerce_course(recipe: dict) -> None:
    """Force recipe['course'] to a valid enum value; invalid/missing → ''."""
    recipe["course"] = _valid_course(recipe.get("course"))

router = APIRouter()

# ── Draft → recipe coercion ───────────────────────────────────────────────────
def _unwrap(f):
    """Draft fields are {value, source}; return the bare value (or f if not wrapped)."""
    return f.get("value") if isinstance(f, dict) and "value" in f and "source" in f else f

def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

def _recipe_from_draft(draft: dict, source: dict) -> dict:
    """Turn an AI extraction draft (per-field provenance) into a saveable recipe,
    accepting both extracted and suggested values (auto-import; reviewed later)."""
    g = lambda k: _unwrap(draft.get(k))
    def ing(i):
        o = _unwrap(i) or {}
        f = lambda k: _unwrap(o.get(k))   # sub-fields are provenance-wrapped too
        return {"amount": str(f("amount") if f("amount") is not None else ""),
                "unit": str(f("unit") or ""), "item": str(f("item") or ""),
                "notes": str(f("notes") or ""), "category": (f("category") or "")}
    def step(s):
        o = _unwrap(s) or {}
        f = lambda k: _unwrap(o.get(k))
        return {"text": str(f("text") or ""), "duration_min": _to_int(f("duration_min"))}
    return {
        "name":          (str(g("name") or "").strip() or "Imported recipe"),
        "description":   str(g("description") or ""),
        "cuisine":       str(g("cuisine") or ""),
        "course":        _valid_course(g("course")),
        "tags":          g("tags") or [],
        "meal_type":     g("meal_type") or [],
        "dietary":       g("dietary") or [],
        "servings":      _to_int(g("servings")),
        "difficulty":    _to_int(g("difficulty")),
        "prep_time_min": _to_int(g("prep_time_min")),
        "cook_time_min": _to_int(g("cook_time_min")),
        "ingredients":   [ing(i) for i in (g("ingredients") or [])],
        "equipment":     g("equipment") or [],
        "steps":         [step(s) for s in (g("steps") or [])],
        "notes":         str(g("notes") or ""),
        "source":        source,
        "log":           {"entered_by": "", "entered_at": date.today().isoformat()},
    }

# ── Duplicate detection (F8b) ─────────────────────────────────────────────────
def _fold(s) -> str:
    """Diacritic-fold + lowercase + strip punctuation (Python mirror of the
    frontend fold()), so 'Köfte' and 'kofte' compare equal."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("ı", "i").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())

def _draft_name(draft: dict) -> str:
    return str(_unwrap(draft.get("name")) or "")

def _draft_ingredient_names(draft: dict) -> list:
    out = []
    for i in (_unwrap(draft.get("ingredients")) or []):
        o = _unwrap(i) or {}
        it = _unwrap(o.get("item"))
        if it and str(it).strip():
            out.append(str(it))
    return out

def find_similar(name: str, source: dict, ingredients=None, limit: int = 3) -> list:
    """Offline duplicate detection (no AI). Returns [{id, name, score, reason}]
    strongest-first: (1) same source identity (notion page title / url), (2)
    folded-name ratio ≥ 0.75, (3) name ratio ≥ 0.6 AND ingredient Jaccard ≥ 0.5."""
    source = source or {}
    stype  = (source.get("type") or "").strip().lower()
    svalue = (source.get("value") or "").strip()
    nfold  = _fold(name)
    ings   = {_fold(i) for i in (ingredients or []) if str(i).strip()}
    out = []
    for e in read_recipe_index():
        est = (e.get("source_type") or "").strip().lower()
        esv = (e.get("source_value") or "").strip()
        if (stype in ("notion", "url", "instagram") and stype == est
                and svalue and svalue == esv):
            out.append({"id": e["id"], "name": e.get("name", ""), "score": 1.0,
                        "reason": "same source"})
            continue
        ratio = difflib.SequenceMatcher(None, nfold, _fold(e.get("name", ""))).ratio() if nfold else 0.0
        if ratio >= 0.75:
            out.append({"id": e["id"], "name": e.get("name", ""), "score": round(ratio, 3),
                        "reason": "similar name"})
            continue
        if ings and ratio >= 0.6:
            eset = {_fold(x) for x in (e.get("ingredients") or []) if str(x).strip()}
            if eset:
                jac = len(ings & eset) / len(ings | eset)
                if jac >= 0.5:
                    out.append({"id": e["id"], "name": e.get("name", ""), "score": round(jac, 3),
                                "reason": "shared ingredients"})
    out.sort(key=lambda m: -m["score"])
    return out[:limit]

# ── Pending import drafts (F8c) ───────────────────────────────────────────────
_PENDING_META = {"pendingId", "matchId", "matchName", "matchScore", "created", "who"}
# Fields the merge UI can overwrite (id/log/rating are always preserved; photos
# are always unioned). "time" is a pseudo-field covering the three time keys.
_MERGE_FIELDS = {"name", "description", "ingredients", "steps", "notes", "variations",
                 "equipment", "servings", "cuisine", "course", "tags", "meal_type",
                 "dietary", "source"}

def _queue_pending(recipe: dict, match: dict, who: str) -> str:
    """Write a pending draft (the full recipe + match metadata). Caller holds the
    write lock. Returns the pendingId."""
    pid = uuid.uuid4().hex[:12]
    draft = dict(recipe)
    draft.update({"pendingId": pid, "matchId": match.get("id", ""),
                  "matchName": match.get("name", ""), "matchScore": match.get("score"),
                  "created": datetime.now().isoformat(timespec="seconds"),
                  "who": who or recipe.get("log", {}).get("entered_by", "")})
    write_pending_recipe(pid, draft)
    return pid

# ── SSRF-guarded page fetch ───────────────────────────────────────────────────
def _host_is_public(host: str) -> bool:
    """True only if every IP the host resolves to is a global (public) address —
    blocks SSRF to loopback/private/link-local/cloud-metadata targets."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return False
    return True

async def _fetch_url_text(url: str) -> str:
    """SSRF-safe fetch of a recipe page, returned as text for the extractor.
    Validates the host resolves to a public IP at every redirect hop, caps size."""
    hop = url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False,
                                 headers={"User-Agent": "FamilyCalendar/1.0 (+recipe import)"}) as client:
        for _ in range(4):   # bounded redirect chain, each hop re-validated
            parsed = urlparse(hop)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(400, "Only http(s) URLs are allowed")
            if not _host_is_public(parsed.hostname):
                log_event("connectivity", "url.fetch", f"Blocked non-public URL: {url}", level="warn")
                raise HTTPException(400, "That URL isn't allowed")
            try:
                resp = await client.get(hop)
            except Exception as e:
                log_event("connectivity", "url.fetch", f"Fetch failed for {parsed.hostname}: {e}",
                          level="error", detail={"url": url})
                raise HTTPException(502, "Could not fetch that page")
            if resp.is_redirect and resp.headers.get("location"):
                hop = str(resp.url.join(resp.headers["location"]))
                continue
            if resp.status_code != 200:
                log_event("connectivity", "url.fetch", f"Page returned {resp.status_code}: {url}",
                          level="warn", detail={"status": resp.status_code})
                raise HTTPException(502, f"Could not fetch the page ({resp.status_code})")
            return _html_to_text(resp.text[:600_000])   # cap raw HTML before stripping
    raise HTTPException(502, "Too many redirects")

def _meta_content(html: str, key: str) -> str:
    """Value of a <meta property|name="key" content="…"> tag, either attribute order."""
    k = re.escape(key)
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{k}["\'][^>]*content=["\'](.*?)["\']',
                rf'<meta[^>]+content=["\'](.*?)["\'][^>]*(?:property|name)=["\']{k}["\']'):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return unescape(m.group(1)).strip()
    return ""

def _html_to_text(html: str) -> str:
    """Reduce a web page to recipe-relevant text: keep social/preview meta tags
    (Instagram & others expose the caption/summary in og:description), keep JSON-LD
    blocks (recipe sites embed the full recipe there), drop scripts/styles, strip tags."""
    meta = "\n".join(t for t in (_meta_content(html, "og:title"),
                                 _meta_content(html, "og:description"),
                                 _meta_content(html, "description")) if t)
    ld = "\n".join(re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S))
    body = re.sub(r'<(script|style|noscript|template)[^>]*>.*?</\1>', ' ', html, flags=re.I | re.S)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = re.sub(r'&nbsp;', ' ', body)
    body = re.sub(r'[ \t]+', ' ', body)
    body = re.sub(r'\n\s*\n+', '\n', body).strip()
    return (meta + "\n" + ld + "\n" + body).strip()

# ── AI extraction prompts ─────────────────────────────────────────────────────
# Shared clause describing the course enum (F4), reused across the prompts.
_COURSE_CLAUSE = ("course (the kind of dish — EXACTLY ONE of: "
                  + ", ".join(RECIPE_COURSES) + ")")

EXTRACT_SYSTEM = (
    "You extract a structured recipe from a photo of a recipe (cookbook page, "
    "recipe card, or handwritten note). Return ONLY a JSON object, no markdown. "
    'For EVERY field, wrap the value with provenance: '
    '{"value": <value>, "source": "extracted" | "suggested" | "empty"}.\n'
    '- "extracted": read directly from the photo.\n'
    '- "suggested": not in the photo, but a sensible inference '
    '(e.g. cuisine, tags, difficulty, meal_type).\n'
    '- "empty": nothing found and nothing worth inferring — use value "" or [].\n'
    'Prefer a confident estimate to leaving a field blank: when it is not shown, infer '
    'cuisine, tags, meal_type, dietary, ' + _COURSE_CLAUSE + ', difficulty (1-5), servings '
    '(from the ingredient quantities), equipment (the pans/tools the steps imply), and '
    'prep_time_min / cook_time_min (from the number and nature of the steps), marking each '
    'such value "suggested". Use "empty" only when you truly cannot tell.\n'
    "The photo may be in any language (commonly English, Turkish, or Swedish). KEEP the "
    "content fields in the ORIGINAL language of the source — do NOT translate name, "
    "description, ingredient items (item), ingredient notes, steps, notes, or variations. "
    "Output the TAXONOMY fields in English: cuisine, tags, meal_type, dietary, course, and "
    "each ingredient's aisle category. Units may be normalized but keep the ingredient "
    "wording otherwise verbatim.\n"
    "Fields (use exactly these keys): name, description, cuisine, " + _COURSE_CLAUSE + ", "
    "tags (array), meal_type (array), dietary (array), servings (number), difficulty (1-5), "
    "prep_time_min (number), cook_time_min (number), "
    "ingredients (array of {amount, unit, item, notes, category}), "
    "equipment (array), steps (array of {text, duration_min}), notes, "
    "source (object with keys {type, value, author, publisher, page}).\n"
    "The \"source\" is the recipe's origin, read from the photo ONLY if shown: type is one of "
    '"book", "magazine", "website", "handwritten", "unknown"; value is the book or magazine '
    "title (or the website); author, publisher, and page are filled when printed on the page "
    '(e.g. a cookbook page footer). Wrap each source sub-field with provenance like any other '
    'field. If no origin is visible, use type "unknown" and empty strings.\n'
    "Each ingredient's \"category\" is the supermarket aisle it belongs to (mark it "
    '"suggested"), EXACTLY ONE of: ' + ", ".join(SHOP_CATEGORIES) + ".\n"
    "For measurements: use grams/kilograms (g, kg) for weight and Celsius for temperature "
    "— convert pounds, ounces, and Fahrenheit even if the photo uses them (mark converted "
    "values as \"extracted\"). For volume, keep cups, tablespoons (tbsp), and teaspoons (tsp) "
    "as a home cook measures — do NOT convert those to millilitres. Convert inches to cm."
)

EXTRACT_TEXT_SYSTEM = (
    "You extract a structured recipe from recipe text — pasted by a user or the "
    "readable content of a web page. Return ONLY a JSON object, no markdown. "
    'For EVERY field, wrap the value with provenance: '
    '{"value": <value>, "source": "extracted" | "suggested" | "empty"}.\n'
    '- "extracted": present in the provided text.\n'
    '- "suggested": not stated, but a sensible inference (e.g. cuisine, tags, difficulty, meal_type).\n'
    '- "empty": nothing found and nothing worth inferring — use value "" or [].\n'
    'Prefer a confident estimate to leaving a field blank: when it is not stated, infer '
    'cuisine, tags, meal_type, dietary, ' + _COURSE_CLAUSE + ', difficulty (1-5), servings '
    '(from the ingredient quantities), equipment (the pans/tools the steps imply), and '
    'prep_time_min / cook_time_min (from the number and nature of the steps), marking each '
    'such value "suggested". Use "empty" only when you truly cannot tell.\n'
    "The page may contain navigation, ads, or comments — ignore everything that isn't the recipe.\n"
    "The source may be in any language (commonly English, Turkish, or Swedish). KEEP the "
    "content fields in the ORIGINAL language of the source — do NOT translate name, "
    "description, ingredient items (item), ingredient notes, steps, notes, or variations. "
    "Output the TAXONOMY fields in English: cuisine, tags, meal_type, dietary, course, and "
    "each ingredient's aisle category. Units may be normalized but keep the ingredient "
    "wording otherwise verbatim.\n"
    "Social-media captions (e.g. Instagram) often begin with like/comment counts, the author's "
    "handle and date, then the recipe, then hashtags, emoji, and calls to action ('comment "
    "RECIPE', 'link in bio') — ignore all of that framing and extract only the recipe. If the "
    'text contains no actual recipe (no ingredients or steps), set name to {"value": "", "source": "empty"}.\n'
    "Fields (use exactly these keys): name, description, cuisine, " + _COURSE_CLAUSE + ", "
    "tags (array), meal_type (array), dietary (array), servings (number), difficulty (1-5), "
    "prep_time_min (number), cook_time_min (number), "
    "ingredients (array of {amount, unit, item, notes, category}), "
    "equipment (array), steps (array of {text, duration_min}), notes.\n"
    "Each ingredient's \"category\" is the supermarket aisle it belongs to (mark it "
    '"suggested"), EXACTLY ONE of: ' + ", ".join(SHOP_CATEGORIES) + ".\n"
    "For measurements: use grams/kilograms (g, kg) for weight and Celsius for temperature "
    "— convert pounds, ounces, and Fahrenheit even if the source uses them (mark converted "
    "values as \"extracted\"). For volume, keep cups, tablespoons (tbsp), and teaspoons (tsp) "
    "as a home cook measures — do NOT convert those to millilitres. Convert inches to cm."
)

# Language: content fields stay in the source language by default (F8a). Imports may
# opt in to translation (translate=True) — the keep-original clause (identical in both
# prompts above) is swapped for the translate clause at call time.
_KEEP_LANG_CLAUSE = (
    "KEEP the content fields in the ORIGINAL language of the source — do NOT translate name, "
    "description, ingredient items (item), ingredient notes, steps, notes, or variations. "
    "Output the TAXONOMY fields in English: cuisine, tags, meal_type, dietary, course, and "
    "each ingredient's aisle category. Units may be normalized but keep the ingredient "
    "wording otherwise verbatim.\n")
_TRANSLATE_LANG_CLAUSE = (
    "TRANSLATE the content fields into ENGLISH — output name, description, ingredient items "
    "(item), ingredient notes, steps, notes, and variations in English (do not keep the "
    "original-language text). The TAXONOMY fields (cuisine, tags, meal_type, dietary, course, "
    "and each ingredient's aisle category) are English too. Keep numeric amounts and units.\n")

def extract_system(translate: bool = False) -> str:
    return EXTRACT_SYSTEM.replace(_KEEP_LANG_CLAUSE, _TRANSLATE_LANG_CLAUSE) if translate else EXTRACT_SYSTEM

def extract_text_system(translate: bool = False) -> str:
    return EXTRACT_TEXT_SYSTEM.replace(_KEEP_LANG_CLAUSE, _TRANSLATE_LANG_CLAUSE) if translate else EXTRACT_TEXT_SYSTEM

# A full recipe draft (ingredients + steps + taxonomy, with per-field provenance)
# runs long. At the old 2000 the model was silently truncated mid-JSON on ordinary
# recipes — one measured extraction came in at 1952, i.e. a 48-token margin — and
# the only symptom was a parse error the relay drain then retried forever.
EXTRACT_MAX_TOKENS = int(os.getenv("RECIPE_EXTRACT_MAX_TOKENS", "4000"))


async def _ai_extract_recipe_text(text: str, translate: bool = False) -> dict:
    """LLM call shared by the interactive route and the relay import queue.
    Returns the per-field-provenance draft; raises on parse failure."""
    text = text[:16000]
    raw = await ai.complete(extract_text_system(translate),
                            [{"role": "user", "content": f"Recipe source:\n{text}"}],
                            EXTRACT_MAX_TOKENS, action="recipe.extract_text", timeout=60)
    return parse_ai_json(raw, "recipe.extract_text")

async def _import_recipe(p: dict):
    """Fetch (URL) or take (text) a recipe, AI-extract it, and save it to the library.
    Used by the relay drain; reuses the same SSRF-guarded fetch + extractor as the
    interactive route."""
    url  = (p.get("url") or "").strip()
    text = (p.get("text") or "").strip()
    if url:
        text = await _fetch_url_text(url)          # SSRF-guarded
        source = {"type": "url", "value": url}
    else:
        source = {"type": "text", "value": ""}
    if not text:
        return
    draft  = await _ai_extract_recipe_text(text, bool(p.get("translate")))   # may raise → retried
    recipe = _recipe_from_draft(draft, source)
    async with _write_lock:
        recipe["id"] = _slugify(recipe["name"])
        write_recipe_file(recipe["id"], recipe)
        _upsert_index(recipe)
        build_display_cache()
    await broadcast("update", {"section": "recipes"})


_PHOTO_CMD_MAX = 2_000_000   # decoded bytes; relay MAX_BODY caps the wire anyway


async def _import_recipe_photo(p: dict):
    """A recipe photographed on the relay phone app (queued command, no review
    step). AI-extracts it in its original language and saves it straight to the
    library flagged `needs_review` — the admin Recipe editor highlights it until
    a human saves it. A likely duplicate is queued under Pending imports instead
    (same F8c flow as re-imports). Bad image data is dropped (logged), so a
    corrupt command can't become a retry poison pill; AI failures raise → retried."""
    try:
        data = base64.b64decode((p.get("image") or "").strip(), validate=True)
    except Exception:
        data = b""
    if not data or len(data) > _PHOTO_CMD_MAX:
        log_event("import", "recipe.photo", "Dropped relay photo command (bad or oversized image)",
                  level="warn", detail={"bytes": len(data)})
        return
    media = p.get("media") or "image/jpeg"
    ext   = "png" if "png" in media else "jpg"
    draft  = await _ai_extract_recipe_image([(data, media, ext)], bool(p.get("translate")))
    recipe = _recipe_from_draft(draft, {"type": "photo", "value": ""})
    who = str(p.get("who") or "")
    recipe["log"]["entered_by"] = who
    matches = find_similar(recipe.get("name", ""), recipe.get("source") or {},
                           [i.get("item", "") for i in recipe.get("ingredients", [])])
    async with _write_lock:
        if matches:
            pid = _queue_pending(recipe, matches[0], who)
            log_event("import", "recipe.photo",
                      f"Phone scan '{recipe.get('name', '')}' matches '{matches[0]['name']}' — queued for merge",
                      who=who, detail={"pendingId": pid, "matchId": matches[0]["id"]})
        else:
            recipe["id"] = _unique_recipe_id(recipe.get("name", "recipe"))
            recipe["needs_review"] = True
            _attach_photo_bytes(recipe, data, ext, who)
            write_recipe_file(recipe["id"], recipe)
            _upsert_index(recipe)
            build_display_cache()
            log_event("import", "recipe.photo",
                      f"Phone scan saved '{recipe.get('name', '')}' (flagged for review)",
                      who=who, detail={"id": recipe["id"]})
    await broadcast("update", {"section": "recipes"})

# ── Routes — recipe CRUD ──────────────────────────────────────────────────────
@router.post("/recipes/reindex")
async def reindex_recipes():
    """Regenerate index.json from the recipe files (picks up new index fields)."""
    async with _write_lock:
        n = rebuild_recipe_index()
    log_event("data", "recipe.reindex", f"Rebuilt recipe index ({n} recipes)")
    return {"ok": True, "count": n}

@router.get("/recipes")
def get_recipe_index():
    return read_recipe_index()

# NOTE: the specific /recipes/* GET routes below must precede /recipes/{recipe_id},
# or FastAPI would match "similar"/"pending" as a recipe id.
@router.get("/recipes/similar")
def similar_recipes(name: str = "", source_type: str = "", source_value: str = ""):
    """Ad-hoc duplicate check for the editor (F8b)."""
    return {"matches": find_similar(name, {"type": source_type, "value": source_value})}

@router.get("/recipes/pending")
def get_pending():
    """Summary rows for the queued import drafts awaiting merge (F8c)."""
    rows = []
    for d in list_pending_recipes():
        rows.append({"pendingId": d.get("pendingId", ""), "name": d.get("name", ""),
                     "matchId": d.get("matchId", ""), "matchName": d.get("matchName", ""),
                     "matchScore": d.get("matchScore"), "created": d.get("created", ""),
                     "who": d.get("who", "")})
    rows.sort(key=lambda r: r.get("created", ""))
    return rows

@router.get("/recipes/pending/{pid}")
def get_pending_one(pid: str):
    d = read_pending_recipe(pid)
    if d is None:
        raise HTTPException(404, "Pending import not found")
    return d

@router.post("/recipes/pending")
async def create_pending(request: Request):
    """Queue a draft for merge against an existing match (shared by review flows)."""
    body   = await request.json()   # {recipe: {...}, matchId, matchName?, matchScore?, who?}
    recipe = body.get("recipe") or {}
    if not recipe.get("name"):
        raise HTTPException(400, "A recipe with a name is required")
    match = {"id": body.get("matchId", ""), "name": body.get("matchName", ""),
             "score": body.get("matchScore")}
    async with _write_lock:
        pid = _queue_pending(recipe, match, body.get("who", ""))
    log_event("import", "recipe.pending", f"Queued '{recipe.get('name', '')}' for merge",
              detail={"pendingId": pid, "matchId": match["id"]})
    return {"ok": True, "pendingId": pid}

@router.post("/recipes/pending/{pid}/resolve")
async def resolve_pending(pid: str, request: Request):
    """Resolve a pending draft: merge chosen fields into the match, save as new, or
    discard. Body: {action: 'merge'|'create'|'discard', fields?: [...]}."""
    body   = await request.json()
    action = (body.get("action") or "").strip()
    draft  = read_pending_recipe(pid)
    if draft is None:
        raise HTTPException(404, "Pending import not found")
    recipe_draft = {k: v for k, v in draft.items() if k not in _PENDING_META}

    if action == "discard":
        delete_pending_recipe(pid)
        log_event("import", "recipe.merge", f"Discarded pending '{draft.get('name', '')}'",
                  detail={"pendingId": pid, "action": "discard"})
        return {"ok": True, "action": "discard"}

    if action == "create":
        async with _write_lock:
            recipe_draft["id"] = _unique_recipe_id(recipe_draft.get("name", "recipe"))
            recipe_draft.setdefault("log", {"entered_by": draft.get("who", ""),
                                            "entered_at": date.today().isoformat()})
            _coerce_course(recipe_draft)
            write_recipe_file(recipe_draft["id"], recipe_draft)
            _upsert_index(recipe_draft)
            delete_pending_recipe(pid)          # same lock: resolve is all-or-nothing
            build_display_cache()
        await broadcast("update", {"section": "recipes"})
        log_event("import", "recipe.merge", f"Saved pending '{recipe_draft.get('name', '')}' as new",
                  detail={"pendingId": pid, "action": "create", "id": recipe_draft["id"]})
        return {"ok": True, "action": "create", "id": recipe_draft["id"]}

    if action == "merge":
        fields  = [f for f in (body.get("fields") or [])]
        matchId = draft.get("matchId", "")
        async with _write_lock:
            existing = read_recipe_file(matchId)
            if existing is None:
                raise HTTPException(404, "The matched recipe no longer exists")
            for f in fields:
                if f == "time":
                    for k in ("prep_time_min", "cook_time_min", "inactive_time_min"):
                        if k in recipe_draft:
                            existing[k] = recipe_draft[k]
                elif f in _MERGE_FIELDS and f in recipe_draft:
                    existing[f] = recipe_draft[f]
            # Always preserve identity/provenance/rating; union photos.
            existing["id"] = matchId
            photos = list(existing.get("photos", []) or [])
            for p in (recipe_draft.get("photos", []) or []):
                if p not in photos:
                    photos.append(p)
            if photos:
                existing["photos"] = photos
            _coerce_course(existing)
            write_recipe_file(matchId, existing)
            _upsert_index(existing)
            delete_pending_recipe(pid)          # same lock: resolve is all-or-nothing
            build_display_cache()
        await broadcast("update", {"section": "recipes"})
        log_event("import", "recipe.merge", f"Merged pending into '{existing.get('name', '')}'",
                  detail={"pendingId": pid, "action": "merge", "id": matchId, "fields": fields})
        return {"ok": True, "action": "merge", "id": matchId, "fields": fields}

    raise HTTPException(400, "Unknown action (expected merge / create / discard)")

@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: str):
    r = read_recipe_file(recipe_id)
    if r is None:
        raise HTTPException(404, "Recipe not found")
    return r

@router.post("/recipes")
async def create_recipe(request: Request):
    recipe = await request.json()
    async with _write_lock:
        if not recipe.get("id"):
            recipe["id"] = _slugify(recipe.get("name", "recipe"))
        _coerce_course(recipe)
        pending_photo = recipe.pop("_pending_photo", None)
        photo_who     = recipe.pop("_photo_who", "")
        if pending_photo:
            _attach_pending_photo(recipe, pending_photo, photo_who)
        write_recipe_file(recipe["id"], recipe)
        _upsert_index(recipe)
    src = recipe.get("source") if isinstance(recipe.get("source"), dict) else {}
    log_event("data", "recipe.create", f"Created recipe '{recipe.get('name', '')}'",
              detail={"id": recipe["id"], "source": src.get("type", "")})
    return {"ok": True, "id": recipe["id"]}

@router.put("/recipes/{recipe_id}")
async def update_recipe(recipe_id: str, request: Request):
    recipe = await request.json()
    async with _write_lock:
        recipe["id"] = recipe_id
        # A manual save from the editor IS the review — clear the phone-scan flag.
        recipe.pop("needs_review", None)
        _coerce_course(recipe)
        write_recipe_file(recipe_id, recipe)
        _upsert_index(recipe)
        build_display_cache()   # a planned recipe's details may be shown on the device
    await broadcast("update", {"section": "recipes"})
    log_event("data", "recipe.update", f"Updated recipe '{recipe.get('name', '')}'",
              detail={"id": recipe_id})
    return {"ok": True}

@router.delete("/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str):
    async with _write_lock:
        existing = read_recipe_file(recipe_id)
        delete_recipe_file(recipe_id)
        index = [e for e in read_recipe_index() if e["id"] != recipe_id]
        write_recipe_index(index)
        build_display_cache()
    await broadcast("update", {"section": "recipes"})
    if existing:
        log_event("data", "recipe.delete", f"Deleted recipe '{existing.get('name', '')}'",
                  detail={"id": recipe_id})
    else:
        log_event("data", "recipe.delete", f"Delete requested for unknown recipe '{recipe_id}'",
                  level="warn", detail={"id": recipe_id})
    return {"ok": True}

# ── Routes — photo/text extraction ────────────────────────────────────────────
@router.post("/recipes/extract")
async def extract_recipe(request: Request):
    """Take a photo (base64), persist it, and return an AI-extracted recipe draft
    with per-field provenance. The photo is held in _pending/ until the recipe is
    saved via POST /recipes with {_pending_photo: <photo_id>}."""
    ai.ensure_ready()
    body       = await request.json()   # {image: <base64>, media_type, who}
    b64        = body.get("image", "")
    media_type = body.get("media_type", "image/jpeg")
    if not b64:
        raise HTTPException(400, "No image provided")

    # 1. Persist the photo immediately so it survives even if extraction fails.
    photo_id = uuid.uuid4().hex[:8]
    ext      = media_type.split("/")[-1].replace("jpeg", "jpg")
    try:
        (PENDING_DIR / f"{photo_id}.{ext}").write_bytes(base64.b64decode(b64))
    except Exception:
        raise HTTPException(400, "Invalid image data")

    # 2. Vision call — image block BEFORE the text block (the provider layer
    # translates it to each API's image format).
    raw = await ai.complete(extract_system(bool(body.get("translate"))), [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": "Extract this recipe."},
    ]}], EXTRACT_MAX_TOKENS, action="recipe.extract_photo", timeout=60)
    try:
        draft = parse_ai_json(raw, "recipe.extract_photo")
    except Exception:
        raise HTTPException(500, "Couldn't read a recipe from that photo")
    log_event("import", "recipe.extract_photo", "Extracted a recipe draft from a photo", detail={"photo_id": photo_id})
    matches = find_similar(_draft_name(draft), _unwrap(draft.get("source")) or {},
                           _draft_ingredient_names(draft))
    return {"draft": draft, "photo_id": photo_id, "matches": matches}

@router.post("/recipes/extract-text")
async def extract_recipe_text(request: Request):
    """Extract a recipe draft from a pasted recipe {text} or a {url}. Same draft
    shape as /recipes/extract (per-field provenance), but no photo."""
    ai.ensure_ready()
    body = await request.json()   # {text?, url?}
    url  = (body.get("url") or "").strip()
    text = (body.get("text") or "").strip()
    if url:
        text = await _fetch_url_text(url)
    if not text:
        raise HTTPException(400, "No recipe text or URL provided")
    try:
        draft = await _ai_extract_recipe_text(text, bool(body.get("translate")))
    except Exception:
        raise HTTPException(500, "Couldn't read a recipe from that")
    src = _source_for_url(url) if url else {"type": "text", "value": ""}
    matches = find_similar(_draft_name(draft), src, _draft_ingredient_names(draft))
    return {"draft": draft, "source_url": url, "matches": matches}

@router.post("/recipes/import-link")
async def import_link(request: Request):
    """Import ONE recipe from a {url} (recipe site or Instagram post) or pasted {text},
    save it, and return {id, name} to open in the editor (or a pending match to merge).
    Content is kept in the source's original language; the editor is the review step."""
    ai.ensure_ready()
    body = await request.json()
    url  = (body.get("url") or "").strip()
    text = (body.get("text") or "").strip()
    who  = (body.get("who") or "").strip()
    if url:
        src_text = await _fetch_url_text(url)          # SSRF-guarded; keeps og:description
        source   = _source_for_url(url)                # instagram vs website
    else:
        src_text, source = text, {"type": "text", "value": ""}
    if not src_text.strip():
        raise HTTPException(400, "Nothing to import — provide a link or paste the recipe text")
    try:
        draft = await _ai_extract_recipe_text(src_text, bool(body.get("translate")))
    except Exception:
        raise HTTPException(502, "Couldn't read a recipe from that")
    recipe = _recipe_from_draft(draft, source)
    recipe["log"]["entered_by"] = who
    # Instagram posts often gate the recipe ("comment RECIPE") — nothing to import then.
    if not recipe["ingredients"] and not recipe["steps"]:
        log_event("import", "recipe.import_link", f"No recipe found at {source.get('type','link')} link",
                  level="warn", who=who, detail={"source": source})
        raise HTTPException(422, "No recipe found there. If it's an Instagram post, paste the "
                                 "caption text instead — the recipe may not be on the page.")
    on_dup    = (body.get("onDuplicate") or "queue")
    ing_names = [i["item"] for i in recipe["ingredients"] if i.get("item")]
    async with _write_lock:
        matches = [] if on_dup == "create" else find_similar(recipe["name"], source, ing_names, limit=1)
        if matches:
            pid = _queue_pending(recipe, matches[0], who)
        else:
            recipe["id"] = _unique_recipe_id(recipe["name"])
            write_recipe_file(recipe["id"], recipe)
            _upsert_index(recipe)
            build_display_cache()
    if matches:
        log_event("import", "recipe.import_link", f"Queued '{recipe['name']}' for merge",
                  who=who, detail={"pendingId": pid, "matchId": matches[0]["id"],
                                   "matchName": matches[0]["name"]})
        return {"pending": True, "pendingId": pid, "matchId": matches[0]["id"],
                "matchName": matches[0]["name"], "name": recipe["name"]}
    await broadcast("update", {"section": "recipes"})
    log_event("import", "recipe.import_link", f"Imported '{recipe['name']}' from {source.get('type','link')}",
              who=who, detail={"id": recipe["id"], "source": source})
    return {"id": recipe["id"], "name": recipe["name"]}

# ── Bulk import from a Notion "Markdown & CSV" export (.zip) ──────────────────
NOTION_IMPORT_MAX = 80          # recipes processed per import (cost/time guard)
_NOTION_ID_RE = re.compile(r'\s+[0-9a-f]{32}$', re.I)   # trailing Notion page id

def _clean_notion_title(path: str) -> str:
    """`.../Chicken Tagine a1b2…c3d4.md` → `Chicken Tagine`."""
    base = path.rsplit("/", 1)[-1]
    base = re.sub(r'\.md$', '', base, flags=re.I)
    return _NOTION_ID_RE.sub('', base).strip()

def _looks_like_index(md: str) -> bool:
    """A parent/index page is mostly links to child pages with little prose — skip it."""
    body  = re.sub(r'^\s*#.*$', '', md, count=1, flags=re.M)      # drop the H1
    links = len(re.findall(r'\]\([^)]*\.md\)', body))
    prose = re.sub(r'\[[^\]]*\]\([^)]*\)', '', body)              # strip link syntax
    prose = re.sub(r'[#>*_\-\s]', '', prose)                      # strip markdown/space
    return links >= 3 and len(prose) < 60

def _source_for_url(url: str) -> dict:
    """Classify a link as an Instagram post or a general website, for recipe provenance."""
    host  = (urlparse(url).hostname or "").lower()
    is_ig = host == "instagram.com" or host.endswith(".instagram.com")
    return {"type": "instagram" if is_ig else "url", "value": url}

def _first_http_url(md: str) -> str:
    """First external http(s) URL in a note, skipping image/asset links."""
    for m in re.finditer(r'https?://[^\s)>\]"\']+', md):
        u = m.group(0).rstrip('.,);')
        if not re.search(r'\.(png|jpe?g|gif|webp|svg|pdf|mp4|mov)(\?|$)', u, re.I):
            return u
    return ""

def _prose_len(md: str) -> int:
    """Length of a note's real prose, excluding its title, links, URLs, and markdown —
    used to tell a written-out recipe from a page that is essentially just a link."""
    t = re.sub(r'^\s*#.*$', '', md, count=1, flags=re.M)   # drop the H1 title
    t = re.sub(r'\[[^\]]*\]\([^)]*\)', '', t)              # [label](link)
    t = re.sub(r'https?://\S+', '', t)                     # bare URLs
    t = re.sub(r'[#>*_\-\s]', '', t)                       # markdown/space
    return len(t)

_IMG_MD_RE   = re.compile(r'!\[[^\]]*\]\(([^)\s]+)')
# Notion often exports a page's uploaded image as a plain link (no leading `!`),
# so also match [label](path.png) where the path is an image file.
_IMG_LINK_RE = re.compile(r'\[[^\]]*\]\(([^)\s]+\.(?:png|jpe?g|gif|webp))\)', re.I)
_IMG_SRC_RE  = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_IMG_EXTS    = ("png", "jpg", "jpeg", "gif", "webp")

def _flatten_export(raw: bytes, file_cap: int, total_cap: int, scan_cap: int) -> dict:
    """Flatten a Notion export .zip into {path: bytes}, recursing ONE level into
    nested part-zips (large Notion exports are a zip-of-zips). Enforces per-file,
    cumulative, and member-count caps so a crafted archive can't exhaust memory."""
    out, budget = {}, {"total": 0, "scanned": 0}
    def _walk(data: bytes, prefix: str, depth: int):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            if prefix:                # a nested member that isn't really a zip → skip
                return
            raise HTTPException(400, "That doesn't look like a .zip export")
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            budget["scanned"] += 1
            if budget["scanned"] > scan_cap:
                return
            try:
                size = zf.getinfo(name).file_size
            except Exception:
                continue
            if name.lower().endswith(".zip") and depth > 0:
                if size <= total_cap:
                    try:
                        _walk(zf.open(name).read(total_cap + 1), f"{prefix}{name}!", depth - 1)
                    except HTTPException:
                        raise
                    except Exception:
                        pass
                continue
            if size > file_cap:
                continue
            try:
                b = zf.open(name).read(file_cap + 1)
            except Exception:
                continue
            if len(b) > file_cap:
                continue
            budget["total"] += len(b)
            if budget["total"] > total_cap:
                raise HTTPException(400, "That export decompresses far too large")
            out[f"{prefix}{name}"] = b
    _walk(raw, "", 1)
    return out

def _page_images(lookup: dict, page_name: str, text: str, max_images: int) -> list:
    """(bytes, media_type, ext) for the local image assets a page references.
    Notion keeps them in a folder beside the page and links them by URL-encoded
    path relative to the page — resolve each ref (markdown `![](…)` or HTML
    `<img src>`) against the page's folder to find it in the flattened export."""
    base = posixpath.dirname(page_name)
    refs = [m.group(1) for m in _IMG_MD_RE.finditer(text)] + \
           [m.group(1) for m in _IMG_LINK_RE.finditer(text)] + \
           [m.group(1) for m in _IMG_SRC_RE.finditer(text)]
    seen, out = set(), []
    for ref in refs:
        ref = ref.strip()
        if ref.startswith(("http://", "https://", "data:")):
            continue                                    # external image — can't rely on it
        rel  = urllib.parse.unquote(ref)
        cand = posixpath.normpath(posixpath.join(base, rel) if base else rel)
        hit  = lookup.get(cand.lower().lstrip("./"))
        if not hit or hit[0] in seen:
            continue
        realpath, data = hit
        ext = realpath.rsplit(".", 1)[-1].lower() if "." in realpath else ""
        if ext not in _IMG_EXTS:
            continue
        seen.add(realpath)
        out.append((data, "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}", ext))
        if len(out) >= max_images:
            break
    return out

async def _ai_extract_recipe_image(images: list, translate: bool = False) -> dict:
    """Vision extraction for a screenshot-style page: send the image(s) + a short
    prompt through the same EXTRACT_SYSTEM used by the photo route."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt,
                                            "data": base64.b64encode(d).decode()}}
               for d, mt, _ in images]
    content.append({"type": "text", "text": "Extract this recipe from the screenshot(s)."})
    raw = await ai.complete(extract_system(translate), [{"role": "user", "content": content}],
                            2000, action="recipe.import_notion", timeout=90)
    return parse_ai_json(raw, "recipe.import_notion")

@router.post("/recipes/import-notion")
async def import_notion(request: Request):
    """Bulk-import recipes from a Notion export .zip (base64). Each markdown page is
    AI-extracted into the recipe schema and saved. Returns a per-recipe summary."""
    ai.ensure_ready()
    body   = await request.json()
    b64    = (body.get("zip") or "").strip()
    who    = (body.get("who") or "").strip()
    on_dup = (body.get("onDuplicate") or "queue")   # F8c: queue matches instead of duplicating
    translate = bool(body.get("translate"))
    if b64.startswith("data:"):                      # tolerate a data: URL prefix
        b64 = b64.split(",", 1)[-1]
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "Invalid file data")
    if len(raw) > 40 * 1024 * 1024:                  # compressed upload (stays under the body cap)
        raise HTTPException(400, "File too large (max 40 MB)")

    # Flatten the export — including one level of nested part-zips that large
    # Notion exports use — into {path: bytes}, under decompression-bomb caps.
    FILE_CAP, TOTAL_CAP, SCAN_CAP = 8 * 1024 * 1024, 120 * 1024 * 1024, 20000
    members = _flatten_export(raw, FILE_CAP, TOTAL_CAP, SCAN_CAP)
    lookup  = {p.lower(): (p, b) for p, b in members.items()}

    # Page files: Notion "Markdown & CSV" gives .md; the HTML export gives .html.
    MD_CAP    = 2 * 1024 * 1024
    PAGE_EXTS = (".md", ".markdown", ".html", ".htm")
    candidates, ext_counts = [], {}
    for path, data in members.items():
        base = path.rsplit("/", 1)[-1]
        ext  = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        if ext not in PAGE_EXTS or len(data) > MD_CAP:
            continue
        text = data.decode("utf-8", "replace")
        if ext in (".html", ".htm"):
            text = _html_to_text(text[:600_000])       # reuse the URL-import HTML reducer
        if not text.strip() or _looks_like_index(text):
            continue
        candidates.append((path, _clean_notion_title(path), text))
    if not candidates:
        seen = ", ".join(f"{n}×{e or '(no ext)'}" for e, n in
                         sorted(ext_counts.items(), key=lambda kv: -kv[1])[:6]) or "nothing"
        raise HTTPException(400, "No recipe pages (.md or .html) found in that export "
                                 f"(it contained: {seen}). In Notion, export the page/database "
                                 "as 'Markdown & CSV' (or 'HTML') and re-upload that .zip.")
    truncated  = len(candidates) > NOTION_IMPORT_MAX
    candidates = candidates[:NOTION_IMPORT_MAX]

    # Screenshot pages: a page with almost no prose but referenced image(s) — pull
    # those images (markdown or <img>) from the export, bounded by their own budget.
    IMG_TOTAL_CAP, MAX_PAGE_IMAGES = 60 * 1024 * 1024, 4
    img_total = 0
    pages     = []   # (title, text, images) — images: [(bytes, media_type, ext)]
    for path, title, text in candidates:
        images = []
        if _prose_len(text) < 120:                     # only bother for screenshot-ish pages
            for data, media, ext in _page_images(lookup, path, text, MAX_PAGE_IMAGES):
                if img_total + len(data) > IMG_TOTAL_CAP:
                    break
                img_total += len(data)
                images.append((data, media, ext))
        pages.append((title, text, images))

    sem = asyncio.Semaphore(4)   # a few concurrent AI calls, not a flood
    async def _one(title: str, md: str, images: list) -> dict:
        async with sem:
            if images:
                # Screenshot page → read the recipe from the image(s) by vision.
                source = {"type": "notion", "value": title}
                try:
                    draft = await _ai_extract_recipe_image(images, translate)
                except Exception:
                    return {"ok": False, "name": title}
            else:
                # Text page: if it's essentially just a link (recipe site/Instagram),
                # follow it; otherwise read the note's own text.
                src_text, source = md, {"type": "notion", "value": title}
                url = _first_http_url(md)
                if url and _prose_len(md) < 80:
                    try:
                        src_text = await _fetch_url_text(url)   # SSRF-guarded; keeps og:description
                        source   = _source_for_url(url)         # instagram vs website
                    except Exception:
                        src_text = md                           # unreachable link → fall back to text
                try:
                    draft = await _ai_extract_recipe_text(src_text, translate)
                except Exception:
                    return {"ok": False, "name": title}
            recipe = _recipe_from_draft(draft, source)
            recipe["log"]["entered_by"] = who
            if recipe["name"] in ("", "Imported recipe") and title:
                recipe["name"] = title
            if not recipe["ingredients"] and not recipe["steps"]:
                return {"ok": False, "name": title}   # gated link or no recipe on the page
            # Explicit lock: _one runs concurrently via asyncio.gather, and the
            # match check + unique-id generation + write + index upsert must not
            # interleave between tasks (also dedupes within this same batch).
            ing_names = [i["item"] for i in recipe["ingredients"] if i.get("item")]
            async with _write_lock:
                matches = ([] if on_dup == "create"
                           else find_similar(recipe["name"], source, ing_names, limit=1))
                if matches:
                    pid = _queue_pending(recipe, matches[0], who)
                    return {"ok": True, "pending": True, "name": recipe["name"],
                            "pendingId": pid, "matchName": matches[0]["name"]}
                recipe["id"] = _unique_recipe_id(recipe["name"])
                if images:                            # keep the screenshot with the recipe
                    _attach_photo_bytes(recipe, images[0][0], images[0][2], who)
                write_recipe_file(recipe["id"], recipe)
                _upsert_index(recipe)
            return {"ok": True, "id": recipe["id"], "name": recipe["name"]}

    results  = await asyncio.gather(*[_one(t, m, imgs) for t, m, imgs in pages])
    imported = [r for r in results if r["ok"] and not r.get("pending")]
    pending  = [r for r in results if r["ok"] and r.get("pending")]
    failed   = [r for r in results if not r["ok"]]
    if imported or pending:
        async with _write_lock:
            build_display_cache()
        await broadcast("update", {"section": "recipes"})
    log_event("import", "recipe.import_notion",
              f"Notion import: {len(imported)} imported, {len(pending)} queued for merge, "
              f"{len(failed)} skipped",
              level=("warn" if failed and not imported and not pending else "info"), who=who,
              detail={"imported": [r["name"] for r in imported],
                      "pending": [r["name"] for r in pending],
                      "skipped": [r["name"] for r in failed], "truncated": truncated})
    return {"imported": imported, "pending": pending, "failed": failed, "truncated": truncated}

# ── AI recipe generation + unit normalization ─────────────────────────────────
RECIPE_GEN_SYSTEM = """You generate family dinner recipes as strict JSON — no markdown, no prose.
Return ONLY a JSON array of recipe objects. Each object uses exactly these keys:
{
  "name": "string",
  "description": "string (one short sentence)",
  "cuisine": "string",
  "course": "the kind of dish — EXACTLY ONE of: """ + ", ".join(RECIPE_COURSES) + """",
  "tags": ["string"],
  "meal_type": ["dinner"],
  "dietary": ["string"],
  "servings": 4,
  "difficulty": 1-5,
  "cost": "low" | "medium" | "high",
  "prep_time_min": 0,
  "cook_time_min": 0,
  "creates_leftovers": true | false,
  "ingredients": [{"amount": "string", "unit": "string", "item": "string", "notes": "string", "category": "string"}],
  "equipment": ["string"],
  "steps": [{"text": "string", "duration_min": 0}],
  "notes": "string"
}
Each ingredient's "category" is the supermarket aisle it belongs to, EXACTLY ONE of:
""" + ", ".join(SHOP_CATEGORIES) + """ — so the shopping list groups similar products the way a store is laid out.
Make the recipes realistic and family-friendly for a household of 3, varied across cuisines,
mostly quick weeknight dinners with a couple of longer weekend cooks. Do not repeat any name
listed as already used.
For measurements: use grams/kilograms (g, kg) for weight and Celsius (°C) for temperature.
For volume, measure the way a home cook does — cups, tablespoons (tbsp), teaspoons (tsp) —
rather than millilitres (write "2 cups", not "475 ml"); use ml only for small liquid amounts
where a cup measure would be awkward. Do NOT use pounds, ounces, or inches."""

@router.post("/recipes/ai-generate")
async def ai_generate_recipes(request: Request):
    """Generate AI recipes and save them (marked source=ai). Used to seed the fallback
    pool and for on-demand 'ask AI for a recipe' during planning."""
    ai.ensure_ready()
    body    = await request.json()   # {prompt?, count?}
    count   = max(1, min(int(body.get("count", 1)), 20))
    prompt  = (body.get("prompt") or "varied, family-friendly weeknight dinners").strip()
    created = []
    # Generate in small batches so each response stays parseable.
    for _ in range((count + 4) // 5 + 1):
        if len(created) >= count:
            break
        n     = min(5, count - len(created))
        names = [e["name"] for e in read_recipe_index()]
        avoid = ", ".join(names[-80:]) or "(none)"
        user  = f"Generate {n} recipe(s). Focus: {prompt}. Already used names to avoid: {avoid}."
        raw = await ai.complete_or_none(RECIPE_GEN_SYSTEM, [{"role": "user", "content": user}],
                                        4000, action="recipe.ai_generate", timeout=120)
        if not raw:
            break
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            recipes = json.loads(clean)
        except Exception as e:
            log_event("ai", "recipe.ai_generate", f"Unexpected AI response — not valid JSON ({e})",
                      level="error", detail={"raw": raw[:4000]})
            break
        if not isinstance(recipes, list) or not recipes:
            break
        async with _write_lock:
            for r in recipes:
                if isinstance(r, dict) and r.get("name") and len(created) < count:
                    _coerce_course(r)
                    created.append(_save_ai_recipe(r))
    async with _write_lock:
        build_display_cache()
    await broadcast("update", {"section": "recipes"})
    if created:
        log_event("ai", "recipe.ai_generate", f"Generated {len(created)} recipe(s)",
                  detail={"names": [c["name"] for c in created]})
    return {"created": created, "count": len(created)}

NORMALIZE_UNITS_SYSTEM = """You standardize recipe measurements.
Input is a JSON object: {"ingredients": [{"amount","unit","item","notes"}], "steps": [{"text","duration_min"}]}.
Return the SAME structure with only the measurements changed:
- Weight -> grams (g) or kilograms (kg).
- Temperature in step text -> Celsius (e.g. "400°F" -> "200°C").
- Volume -> cups, tablespoons (tbsp), teaspoons (tsp), the way a home cook measures. Convert
  millilitre/litre amounts to cups where a clean measure fits (240 ml -> 1 cup, 475 ml -> 2 cups,
  120 ml -> 1/2 cup, 60 ml -> 1/4 cup). Keep ml only for small liquid amounts where cups would be
  awkward (e.g. 30 ml).
- Do NOT use pounds, ounces, or inches.
- Keep everything else identical: item names, notes, step wording and order, duration_min, and
  plain counts (e.g. "2 eggs").
Return ONLY the JSON object, no markdown."""

@router.post("/recipes/normalize-units")
async def normalize_recipe_units(request: Request):
    """Standardize recipe units: grams for weight, Celsius, cups/tbsp/tsp for volume. Optional {ids:[...]}."""
    ai.ensure_ready()
    body    = await request.json()
    ids     = body.get("ids") or [e["id"] for e in read_recipe_index()]
    changed = []
    for rid in ids:
        recipe = read_recipe_file(rid)
        if not recipe:
            continue
        payload = {"ingredients": recipe.get("ingredients", []), "steps": recipe.get("steps", [])}
        raw = await ai.complete_or_none(NORMALIZE_UNITS_SYSTEM,
                                        [{"role": "user", "content": json.dumps(payload)}],
                                        2500, action="recipe.normalize_units", timeout=120)
        if not raw:
            break
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            out = json.loads(clean)
        except Exception as e:
            log_event("ai", "recipe.normalize_units", f"Unexpected AI response — not valid JSON ({e})",
                      level="warn", detail={"recipe": rid, "raw": raw[:2000]})
            continue
        async with _write_lock:
            recipe = read_recipe_file(rid)   # re-read: an edit during the AI call must not be lost
            if not recipe:
                continue
            if isinstance(out.get("ingredients"), list):
                recipe["ingredients"] = out["ingredients"]
            if isinstance(out.get("steps"), list):
                recipe["steps"] = out["steps"]
            write_recipe_file(rid, recipe)   # id/source/log/photos/etc. preserved
            _upsert_index(recipe)
        changed.append(rid)
    async with _write_lock:
        build_display_cache()
    await broadcast("update", {"section": "recipes"})
    if changed:
        log_event("ai", "recipe.normalize_units", f"Converted {len(changed)} recipe(s)",
                  detail={"ids": changed})
    return {"converted": changed, "count": len(changed)}


# ── Course classification (F4) ────────────────────────────────────────────────
CLASSIFY_COURSES_SYSTEM = (
    "You classify each recipe into ONE course (the kind of dish). You are given a JSON array "
    "of {id, name, description}. Return ONLY a JSON object mapping each id to EXACTLY ONE "
    "course from this list: " + ", ".join(RECIPE_COURSES) + ". "
    "Guidance: 'main' = the centrepiece of a meal (a dinner/lunch main); 'side' = "
    "accompaniments, dips, sauces; 'soup'; 'salad'; 'dessert' = sweet courses, cakes, "
    "pastries; 'breakfast'; 'baking' = breads and savoury bakes; 'snack'; 'drink'. Use "
    "'other' only when nothing fits. No markdown."
)

# Keyword fallback for the no-API-key path (first match wins; multilingual hints).
_COURSE_KEYWORDS = [
    ("soup",      ("soup", "çorba", "corba", "soppa", "broth", "bisque", "chowder", "stew")),
    ("salad",     ("salad", "sallad", "salata", "slaw", "coleslaw")),
    ("dessert",   ("dessert", "efterrätt", "efterratt", "tatlı", "tatli", "cake", "kek",
                   "tart", "pie", "cookie", "kurabiye", "pudding", "brownie", "baklava",
                   "ice cream", "dondurma", "cheesecake", "mousse")),
    ("breakfast", ("breakfast", "kahvaltı", "kahvalti", "frukost", "pancake", "waffle",
                   "omelet", "omelette", "granola", "porridge", "övernattsgröt")),
    ("baking",    ("bread", "ekmek", "bröd", "brod", "focaccia", "sourdough", "loaf",
                   "scone", "muffin", "roll ", "bun ", "börek", "borek")),
    ("drink",     ("smoothie", "cocktail", "lemonade", "juice", "milkshake", "shake",
                   "içecek", "icecek", "latte", "punch")),
    ("side",      ("side dish", "garnish", "dip", "relish", "pickle", "sauce", "sos",
                   "dressing", "chutney", "meze")),
]

def _keyword_course(name: str, description: str = "") -> str:
    """Best-effort course from name/description keywords; defaults to 'main' since
    the library is dinner-oriented. Only used when the AI is unavailable."""
    hay = f"{name} {description}".lower()
    for course, words in _COURSE_KEYWORDS:
        if any(w in hay for w in words):
            return course
    return "main"

async def _ai_classify_courses(items: list) -> dict:
    """One batched call mapping recipe id → course. Returns {} when no API key or
    the response is unusable; the caller fills gaps with the keyword matcher."""
    if not items:
        return {}
    raw = await ai.complete_or_none(
        CLASSIFY_COURSES_SYSTEM, [{"role": "user", "content": json.dumps(items)}],
        2000, action="recipe.classify_courses", timeout=120)
    if not raw:
        return {}
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        got = json.loads(clean)
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}

@router.post("/recipes/classify-courses")
async def classify_recipe_courses(request: Request):
    """Assign a course to recipes that don't have one — one batched AI call with a
    keyword fallback when the AI is unavailable. Optional {ids:[...]}; default = every
    recipe whose course is missing."""
    body  = await request.json()
    index = read_recipe_index()
    ids   = list(body.get("ids") or [e["id"] for e in index if not (e.get("course") or "")])
    meta, items = {}, []
    for rid in ids:
        r = read_recipe_file(rid)
        if not r:
            continue
        name, desc = r.get("name", ""), (r.get("description") or "")
        meta[rid] = (name, desc)
        items.append({"id": rid, "name": name, "description": desc[:200]})
    if not items:
        return {"classified": [], "count": 0}
    ai_map = await _ai_classify_courses(items)          # network call OUTSIDE the lock
    changed = []
    async with _write_lock:
        for rid, (name, desc) in meta.items():
            recipe = read_recipe_file(rid)              # re-read: edits during the call aren't lost
            if not recipe:
                continue
            course = _valid_course(ai_map.get(rid)) or _keyword_course(name, desc)
            recipe["course"] = course
            write_recipe_file(rid, recipe)
            _upsert_index(recipe)
            changed.append(rid)
        build_display_cache()
    await broadcast("update", {"section": "recipes"})
    if changed:
        log_event("ai", "recipe.classify_courses", f"Classified {len(changed)} recipe(s)",
                  detail={"ids": changed[:50]})
    return {"classified": changed, "count": len(changed)}
