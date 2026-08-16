"""Data model + flat-JSON storage: paths, defaults, the global write lock,
stable item ids, input sanitizers, the recipe file store, and the shopping-aisle
taxonomy. Everything here is dependency-light (only fsatomic + activity_log)
so every other module can import it without cycles."""
import asyncio
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path

import config  # noqa: F401  (loads .env before the env reads below)
from activity_log import log_event
from fsatomic import _atomic_write_text

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA          = Path(os.getenv("DATA_DIR", "/data"))
CACHE         = DATA / "cache"
RECIPES_DIR   = DATA / "recipes"
PHOTOS_DIR    = RECIPES_DIR / "photos"
PENDING_DIR   = PHOTOS_DIR / "_pending"
RECIPE_PENDING_DIR = RECIPES_DIR / "pending"   # F8c: queued import drafts awaiting merge
F_SETTINGS    = DATA / "settings.json"
F_EVENTS      = DATA / "events.json"
F_MEALS       = DATA / "meals.json"
F_SHOPPING    = DATA / "shopping.json"
F_DISPLAY     = CACHE / "display.json"
F_RECIPE_IDX  = RECIPES_DIR / "index.json"
F_RELAY_APPLIED = DATA / "relay_applied.json"   # ids of relay commands already applied (dedupe)
F_RELAY_ATTEMPTS = DATA / "relay_attempts.json" # failed-attempt counts per command id (retry budget)

DATA.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)
RECIPES_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)
PENDING_DIR.mkdir(exist_ok=True)
RECIPE_PENDING_DIR.mkdir(exist_ok=True)

# ── Meal-planner prompt defaults (part of the settings document's DEFAULTS) ────
DEFAULT_INITIAL_PROMPT = (
    "Plan balanced, family-friendly dinners for the week. Keep Monday to Thursday "
    "quick and simple; a longer cook is fine at the weekend. "
    "Across Monday to Friday aim for one chicken dinner, one fish, one red meat, and one "
    "vegetable-focused dinner — vegetable-focused means vegetables are the star of the plate, "
    "it does not have to be vegetarian. The fifth weekday and the weekend are free choice. "
    "If the library cannot cover all four, get as close as you can and say which one is missing "
    "in that day's notes. Avoid repeating the same dish within the week."
)

DEFAULT_SYS_GENERATE = """You are the household's weekly dinner-planning assistant for a family of 3 in Stockholm.

Your task: decide the dinner for each of the 7 days of the week, Monday through Sunday.

Recipe source:
- Choose dishes ONLY from the family's recipe library provided in the user message — those ids/names are the only allowed options. Never invent a recipe that is not in the list. (Only if the library is empty may you suggest sensible family-friendly dinners of your own.)
- Do NOT pick a recipe that appears in the "recently served" list unless the library is too small to fill the week without repeating.
- Vary cuisines and main ingredients across the week; do not use the same recipe twice in one week unless unavoidable.

Days and events:
- Match effort to the day: quicker recipes on busy days, longer or weekend recipes on free days.
- Recipes marked "makes leftovers" can cover the next 1-2 days: after placing one, you MAY set the following day (or two) to "Leftovers: <dish>" reusing the same id, with a short note, instead of cooking again. Leftover days reuse the same recipe on purpose and do NOT count as unwanted repeats.
- Almost every day needs a dinner. An event is context for choosing the recipe — prefer a quicker one when the day looks full — and is not by itself a reason to skip. Swimming, training, a day trip, travel, an appointment, work, a birthday at 11:00: all still need dinner.
- Apply this test before skipping any day: does the event text itself name the evening meal — "dinner", "dinner out", "restaurant", "evening out", invited to eat somewhere? If it does not contain wording like that, plan a dinner, no matter how big or social the event sounds. A daytime party, a celebration, a lunch and a trip all still need dinner.
- When the test passes, return that day with a null id, an empty name, and the note "Dinner out". An evening event that is not about going out or eating — a concert, a late meeting, training — still needs dinner; plan something quick. When in doubt, cook.
- Follow the standing preferences — dietary limits and any variety targets they set for the week — unless the extra request for this week overrides them.

Return ONLY a JSON array of exactly 7 objects, one per day in order Monday to Sunday. Name the day in every object and keep them in order — the day field must match the day you are planning for:
[{"day":"<Monday...Sunday>","id":"<recipe id from the library, or null if the day is skipped>","name":"<recipe name, or empty string if skipped>","notes":"<max 8 words: why it fits, or why the day is skipped>"}]
Always return all 7 days. Use only ids and names that appear in the library. Output no prose and no markdown — just the JSON array."""

DEFAULT_SYS_REFINE = """You are the household's dinner-planning assistant, revising an existing weekly plan for a family of 3 in Stockholm.

You are given: the current 7-day plan (Monday to Sunday), this week's context (which days are busy), the family's recipe library, and a change request from the user.

Apply the change request precisely:
- If it names specific days, change ONLY those days and leave every other day exactly as it is.
- If it asks for a whole-week change, revise the days it affects and keep the rest coherent.
- Choose any replacement ONLY from the recipe library — never invent a dish. Avoid repeating a recipe already used elsewhere in the week unless asked.
- Keep matching effort to the day (quicker recipes on busy days) and keep variety across the week.
- If the request cannot be met from the library, keep the closest available recipe and note the limitation briefly in that day's "notes".

Return ONLY the FULL updated plan as a JSON array of exactly 7 objects, in order Monday to Sunday, INCLUDING the days you did not change:
[{"id":"<recipe id>","name":"<recipe name>","notes":"<max 8 words>"}]
Use only ids and names from the library. Output no prose and no markdown — just the JSON array."""

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULTS = {
    "settings": {
        "familyName": "Our Family",
        # The app's own display name/branding — shown in page titles/headers
        # and as the organizer name on outbound calendar invites. Distinct
        # from familyName (which labels the household inside the app).
        "appName": "Family Calendar",
        # Roles are anonymous + gender-neutral ids; the label is the display name,
        # editable in Settings (the "role → name" mapping).
        "members": [
            {"id": "family","label": "Family","bg": "#000000", "text": "#ffffff"},
            {"id": "owner", "label": "Owner", "bg": "#0000ff", "text": "#ffffff"},
            {"id": "spouse","label": "Spouse","bg": "#ff0000", "text": "#ffffff"},
            {"id": "kid",   "label": "Kid",   "bg": "#00ff00", "text": "#000000"},
        ],
        "display": {"theme": "light", "fontSize": "medium", "showHolidays": True},
        "ai": {"model": "claude-sonnet-4-6"},   # provider/model for all AI features
        "eventColors": {"birthday": "#ffff00", "recurring": "#000000", "holiday": "#ff0000",
                        "birthdayText": "#000000", "holidayText": "#ffffff"},
        "mealPlanner": {
            "initialPrompt":  DEFAULT_INITIAL_PROMPT,
            "systemGenerate": DEFAULT_SYS_GENERATE,
            "systemRefine":   DEFAULT_SYS_REFINE,
            "recentWeeks":    2,   # avoid repeating a dish planned within this many weeks
            "staleWeeks":     6,   # a dish unseen this long is offered back for variety
        },
        # mailbox.org mail/calendar sync (credentials live in env, not here).
        # Existing installs won't re-seed this — mailsync falls back to the
        # same defaults via its _ms_settings() helper.
        "mailSync": {
            "enabled": False, "invitees": [], "defaultWho": "family",
            "autoAccept": False, "syncEvents": True, "syncRecurring": True,
        },
    },
    "events": {
        "events": [
            {"date": "2026-05-12", "who": "kid",    "icon": "swim",    "label": "Swimming"},
            {"date": "2026-05-12", "who": "spouse", "icon": "yoga",    "label": "Yoga"},
            {"date": "2026-05-15", "who": "owner",  "icon": "travel",  "label": "Work trip"},
            {"date": "2026-05-26", "who": "owner",  "icon": "bbq",     "label": "BBQ"},
        ],
        "birthdays": [
            {"name": "Mom's birthday",     "month": 5, "day": 7},
            {"name": "Grandpa's birthday", "month": 5, "day": 14},
            {"name": "Sister's birthday",  "month": 5, "day": 25},
        ],
        "recurring": [
            {"label": "Garbage", "icon": "trash", "startDate": "2026-05-05", "step": 7, "iconOnly": True},
        ],
    },
    "meals": {
        "plan": {},
    },
}

# ── File helpers ──────────────────────────────────────────────────────────────
def read(path: Path, key: str) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            # Corrupt store: log and re-raise. Do NOT silently reset to defaults —
            # that would overwrite the damaged (but maybe recoverable) data.
            log_event("system", "data.corrupt", f"Could not parse {path.name}: {e}", level="error")
            raise
    d = DEFAULTS[key].copy()
    _atomic_write_text(path, json.dumps(d, indent=2, ensure_ascii=False))
    return d

def write(path: Path, data: dict):
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))

# One global write lock serializing every read-modify-write on the JSON stores.
# A single lock (not per-file): traffic is tiny, and one lock makes nested
# acquisition impossible by construction. Without it, correctness depends on
# "no await between read and write" — an invariant any future edit can silently
# break (and AI-in-the-middle flows already did).
# RULE: acquired only by top-level route/loop bodies and the shared
# _add_*/_delete_* helpers; nothing called while it is held may acquire it
# again (asyncio.Lock is NOT reentrant). Keep AI/network calls outside;
# re-read the store inside the lock after any await.
_write_lock = asyncio.Lock()

def read_settings(): return read(F_SETTINGS, "settings")
def read_meals():    return read(F_MEALS,    "meals")

# ── Stable item ids (A7) ───────────────────────────────────────────────────────
# Every event/birthday/recurring item carries a persistent random "id" so items
# can be addressed without racy array indexes (and so sync consumers like
# mailsync can map them). Legacy entries are migrated lazily on first read.
def _new_id() -> str:
    return uuid.uuid4().hex[:8]

def _ensure_ids(ev: dict) -> bool:
    """Assign missing ids in place across all three sections; True if changed."""
    changed = False
    for sec in ("events", "birthdays", "recurring"):
        for item in ev.get(sec, []) or []:
            if isinstance(item, dict) and not item.get("id"):
                item["id"] = _new_id()
                changed = True
    return changed

def read_events() -> dict:
    ev = read(F_EVENTS, "events")
    if _ensure_ids(ev):
        write(F_EVENTS, ev)   # one-time lazy migration; subsequent reads are pure
    return ev

def read_shopping() -> dict:
    return json.loads(F_SHOPPING.read_text()) if F_SHOPPING.exists() else {}

def write_shopping(d: dict):
    _atomic_write_text(F_SHOPPING, json.dumps(d, indent=2, ensure_ascii=False))

# ── Shopping-aisle taxonomy ────────────────────────────────────────────────────
# Canonical shopping aisles in store-walk order. Shared by the AI tagger, the
# keyword fallback, and the frontend render order (frontend keeps its own copy).
SHOP_CATEGORIES = [
    "Produce", "Bakery", "Meat & Fish", "Dairy & Eggs", "Pantry",
    "Spices & Seasoning", "International", "Frozen", "Drinks", "Household", "Other",
]

# Kind of dish (F4) — a different axis from meal_type (occasion). Canonical enum;
# display labels live in the frontends. Missing/invalid is treated as "other".
RECIPE_COURSES = ["main", "side", "soup", "salad", "dessert",
                  "breakfast", "baking", "snack", "drink", "other"]

# Rough shopping-aisle categorisation from the ingredient name (first match wins).
_CATEGORY_KEYWORDS = [
    # Compound pantry items first, so they win over "beef"/"milk"/"fish" below.
    ("Pantry", ["coconut milk","fish sauce","soy sauce","oyster sauce","hoisin","beef broth","chicken broth","vegetable broth","beef stock","chicken stock","vegetable stock","curry paste","tomato paste","tomato sauce","peanut butter"]),
    ("Meat & Fish", ["chicken","beef","pork","lamb","turkey","bacon","sausage","mince","ground beef","ground pork","steak","brisket","guanciale","pancetta","prosciutto","ham","salmon","cod","haddock","tuna","fish","prawn","shrimp","seafood","fillet","tofu"]),
    ("Dairy & Eggs", ["milk","cream","butter","cheese","yogurt","yoghurt","egg","parmesan","pecorino","mozzarella","feta","cheddar","ricotta","mascarpone","creme fraiche","sour cream"]),
    ("Bakery", ["bread","tortilla","naan","pita","baguette","breadcrumb","wrap"]),
    ("Frozen", ["frozen"]),
    ("Spices & Seasoning", ["salt","black pepper","white pepper","peppercorn","paprika","cumin","ground coriander","coriander seed","turmeric","cinnamon","nutmeg","allspice","cardamom","clove","curry powder","chili powder","chilli powder","garam masala","oregano","dried thyme","dried","bay leaf","stock cube","bouillon","seasoning","spice"]),
    ("Produce", ["onion","shallot","garlic","tomato","potato","carrot","bell pepper","peppers","pepper","spinach","lettuce","cucumber","courgette","zucchini","aubergine","eggplant","broccoli","cauliflower","mushroom","lemon","lime","lemongrass","ginger","galangal","chilli","chili","basil","parsley","coriander","cilantro","mint","thyme","rosemary","scallion","spring onion","leek","celery","cabbage","kale","sprout","avocado","apple","banana","corn","peas","green bean"]),
    ("Pantry", ["flour","sugar","rice","pasta","spaghetti","rigatoni","penne","noodle","oil","vinegar","soy sauce","fish sauce","oyster sauce","tomato paste","tomato puree","passata","canned","tinned","coconut milk","broth","stock","beans","lentil","chickpea","honey","mustard","ketchup","mayonnaise","tahini","peanut butter","cornstarch","cornflour","baking powder","yeast","sesame","tamarind","curry paste","wine","sauce"]),
]

def _ingredient_category(item: str) -> str:
    s = (item or "").lower()
    for cat, kws in _CATEGORY_KEYWORDS:
        for kw in kws:
            if kw in s:
                return cat
    return "Other"

# ── Recipe file store ─────────────────────────────────────────────────────────
def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

def _recipe_index_entry(r: dict) -> dict:
    src = r.get("source") if isinstance(r.get("source"), dict) else {}
    prep = int(r.get("prep_time_min", 0) or 0)
    cook = int(r.get("cook_time_min", 0) or 0)
    inactive = int(r.get("inactive_time_min", 0) or 0)
    return {
        "id":           r.get("id", ""),
        "name":         r.get("name", ""),
        "cuisine":      r.get("cuisine", ""),
        "course":       r.get("course", ""),
        "tags":         r.get("tags", []),
        "meal_type":    r.get("meal_type", []),
        "dietary":      r.get("dietary", []),
        "servings":     r.get("servings", 0),
        "difficulty":   r.get("difficulty", 0),
        "prep_time_min": prep,
        "cook_time_min": cook,
        "total_time_min": prep + cook + inactive,
        "creates_leftovers": bool(r.get("creates_leftovers", False)),
        # lowercased ingredient item names, for text search in the viewer (F3).
        "ingredients":  [str((i or {}).get("item", "")).strip().lower()
                         for i in (r.get("ingredients", []) or [])
                         if isinstance(i, dict) and (i.get("item") or "").strip()],
        "equipment":    [str(e).strip().lower() for e in (r.get("equipment", []) or [])
                         if str(e).strip()],
        "source_type":  src.get("type", ""),
        "source_value": src.get("value", ""),
        "rating":       r.get("rating", None),
        "entered_by":   r.get("log", {}).get("entered_by", ""),
        "entered_at":   r.get("log", {}).get("entered_at", ""),
        # Unreviewed phone scans (relay `recipe_photo`); cleared on manual save.
        "needs_review": bool(r.get("needs_review", False)),
    }

def read_recipe_index() -> list:
    if F_RECIPE_IDX.exists():
        return json.loads(F_RECIPE_IDX.read_text())
    return []

def rebuild_recipe_index() -> int:
    """Regenerate index.json from every recipe file on disk (name-sorted). Used by
    the schema-version reindex on startup and the manual POST /recipes/reindex.
    Caller holds the write lock. Returns the number of recipes indexed."""
    index = []
    for path in RECIPES_DIR.glob("*.json"):
        if path.name == F_RECIPE_IDX.name:
            continue
        try:
            index.append(_recipe_index_entry(json.loads(path.read_text())))
        except Exception:
            continue
    index.sort(key=lambda e: e["name"].lower())
    write_recipe_index(index)
    return len(index)

def recipe_index_needs_rebuild() -> bool:
    """Cheap schema-version probe: the index gained keys the old rows lack (F3's
    total_time_min, later `equipment` for search). True when there are recipe
    files but the first index row predates the current schema."""
    idx = read_recipe_index()
    if not idx:
        return False           # empty index rebuilds lazily on first upsert
    return "equipment" not in idx[0]

def write_recipe_index(index: list):
    _atomic_write_text(F_RECIPE_IDX, json.dumps(index, indent=2, ensure_ascii=False))

def read_recipe_file(recipe_id: str) -> dict | None:
    path = RECIPES_DIR / f"{recipe_id}.json"
    return json.loads(path.read_text()) if path.exists() else None

def write_recipe_file(recipe_id: str, recipe: dict):
    _atomic_write_text(RECIPES_DIR / f"{recipe_id}.json",
                       json.dumps(recipe, indent=2, ensure_ascii=False))

def delete_recipe_file(recipe_id: str):
    path = RECIPES_DIR / f"{recipe_id}.json"
    if path.exists():
        path.unlink()

# ── Pending import drafts (F8c) ───────────────────────────────────────────────
def write_pending_recipe(pid: str, draft: dict):
    _atomic_write_text(RECIPE_PENDING_DIR / f"{pid}.json",
                       json.dumps(draft, indent=2, ensure_ascii=False))

def read_pending_recipe(pid: str) -> dict | None:
    path = RECIPE_PENDING_DIR / f"{pid}.json"
    return json.loads(path.read_text()) if path.exists() else None

def list_pending_recipes() -> list:
    out = []
    for p in sorted(RECIPE_PENDING_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out

def delete_pending_recipe(pid: str):
    (RECIPE_PENDING_DIR / f"{pid}.json").unlink(missing_ok=True)

def _upsert_index(recipe: dict):
    rid = recipe["id"]
    index = [e for e in read_recipe_index() if e["id"] != rid]
    index.append(_recipe_index_entry(recipe))
    index.sort(key=lambda e: e["name"].lower())
    write_recipe_index(index)

def _unique_recipe_id(name: str) -> str:
    base = _slugify(name) or "recipe"
    ids  = {e["id"] for e in read_recipe_index()}
    if base not in ids:
        return base
    n = 2
    while f"{base}-{n}" in ids:
        n += 1
    return f"{base}-{n}"

def _save_ai_recipe(recipe: dict) -> dict:
    """Persist an AI-generated recipe, marked source=ai, with a unique id."""
    recipe["id"]     = _unique_recipe_id(recipe.get("name", "recipe"))
    recipe["source"] = {"type": "ai", "value": ""}
    recipe["log"]    = {"entered_by": "ai", "entered_at": date.today().isoformat()}
    write_recipe_file(recipe["id"], recipe)
    _upsert_index(recipe)
    return {"id": recipe["id"], "name": recipe.get("name", "")}

def _attach_pending_photo(recipe: dict, photo_id: str, who: str):
    """Move a photo from _pending/ into the recipe's folder and record it in photos[]."""
    matches = list(PENDING_DIR.glob(f"{photo_id}.*"))
    if not matches:
        return
    src = matches[0]
    dest_dir = PHOTOS_DIR / recipe["id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src.replace(dest)
    rel = f"{recipe['id']}/{dest.name}"
    recipe.setdefault("photos", []).append({
        "id":       photo_id,
        "path":     f"photos/{rel}",
        "url":      f"/recipe-photos/{rel}",
        "kind":     "source",
        "added_by": who or "",
        "added_at": date.today().isoformat(),
    })


def _attach_photo_bytes(recipe: dict, data: bytes, ext: str, who: str, kind: str = "source"):
    """Write image bytes straight into the recipe's photo folder and record them
    in photos[] (no _pending hop). Used by the Notion import to keep a page's
    screenshot with the recipe it was read from."""
    photo_id = uuid.uuid4().hex[:8]
    ext = (ext or "jpg").lower().lstrip(".").replace("jpeg", "jpg")
    dest_dir = PHOTOS_DIR / recipe["id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{photo_id}.{ext}"
    dest.write_bytes(data)
    rel = f"{recipe['id']}/{dest.name}"
    recipe.setdefault("photos", []).append({
        "id":       photo_id,
        "path":     f"photos/{rel}",
        "url":      f"/recipe-photos/{rel}",
        "kind":     kind,
        "added_by": who or "",
        "added_at": date.today().isoformat(),
    })

# ── Input sanitizers (the gate for all untrusted input: relay + mailsync) ─────
# Icons the remote form may use (mirror of the ICONS list in frontend/mobile.html).
ALLOWED_ICONS = {
    "meeting","doctor","school","football","swim","gym","yoga","travel","bbq","date",
    "music","star","run","cake","trash","beachvolley","pizza","beer","whisky","coffee",
    "cocktail","car","broom",
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_BYDAY   = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def _sanitize_event(p: dict) -> dict | None:
    """Coerce a remote event command into a safe {date, who, icon, label}
    plus optional validated time (HH:MM)."""
    date_s = str(p.get("date") or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_s):
        return None
    label = str(p.get("label") or "").strip()[:80]
    if not label:
        return None
    member_ids = {m.get("id") for m in read_settings().get("members", [])}
    who  = p.get("who") if p.get("who") in member_ids else "family"
    icon = p.get("icon") if p.get("icon") in ALLOWED_ICONS else "star"
    out  = {"date": date_s, "who": who, "icon": icon, "label": label}
    t = str(p.get("time") or "").strip()
    if _TIME_RE.match(t):
        out["time"] = t
    end = str(p.get("endDate") or "").strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", end) and end > date_s:
        out["endDate"] = end   # inclusive last day of a multi-day event
    return out

def _sanitize_recurring(p: dict) -> dict | None:
    """Coerce a remote recurring command into a safe shape. Accepts the legacy
    step-in-days form and the v2 form {freq: daily|weekly, interval, byday,
    until, exdates}; until/exdates are honored for both."""
    start = str(p.get("startDate") or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        return None
    label = str(p.get("label") or "").strip()[:80]
    if not label:
        return None
    icon = p.get("icon") if p.get("icon") in ALLOWED_ICONS else "star"

    # Optional owner: validated member id, or omitted (cache falls back to family).
    member_ids = {m.get("id") for m in read_settings().get("members", [])}
    who = p.get("who") if p.get("who") in member_ids else None

    freq = p.get("freq")
    if freq in ("daily", "weekly"):
        try:
            interval = int(p.get("interval", 1))   # missing → 1; explicit junk/0 → reject below
        except (TypeError, ValueError):
            return None
        if not (1 <= interval <= 520):
            return None
        out = {"label": label, "icon": icon, "startDate": start,
               "freq": freq, "interval": interval, "iconOnly": bool(p.get("iconOnly"))}
        if freq == "weekly":
            byday = [b for b in dict.fromkeys(p.get("byday") or []) if b in _BYDAY]
            if byday:
                out["byday"] = sorted(byday, key=_BYDAY.get)
    else:
        try:
            step = int(p.get("step"))
        except (TypeError, ValueError):
            return None
        if not (1 <= step <= 3650):
            return None
        out = {"label": label, "icon": icon, "startDate": start, "step": step,
               "iconOnly": bool(p.get("iconOnly"))}

    until = str(p.get("until") or "").strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", until) and until >= start:
        out["until"] = until
    ex = sorted({str(x).strip()[:10] for x in (p.get("exdates") or [])
                 if re.match(r"^\d{4}-\d{2}-\d{2}$", str(x).strip()[:10])})
    if ex:
        out["exdates"] = ex[:60]
    t = str(p.get("time") or "").strip()
    if _TIME_RE.match(t):
        out["time"] = t
    if who:
        out["who"] = who
    return out
