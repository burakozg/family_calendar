"""Shopping list: the week's ingredients derived from planned recipes, AI aisle
tagging (persisted on the recipe), check-off state, and the relay publish."""
import json
import re
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request

import ai
import relay_client
import willys
from activity_log import log_event
from bus import broadcast
from relay_client import _log_relay, _relay_headers
from storage import (SHOP_CATEGORIES, _ingredient_category, _write_lock,
                     read_meals, read_recipe_file, read_shopping, write_shopping)

router = APIRouter()

_SHOP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# --- Amount aggregation (F1) -------------------------------------------------
# parse_qty() normalizes a recipe ingredient's amount+unit so the client can sum
# quantities across the selected days. Convertible units are folded into a
# family base unit (mass→grams, volume→ml, spoons/cups→tsp); "count" covers a
# bare number or a size word (summable only when the same word is used).

# raw unit token (lowercased) -> (family, factor to the family base unit)
_UNIT_TABLE = {
    # mass → grams
    "g": ("mass", 1), "gr": ("mass", 1), "gram": ("mass", 1), "grams": ("mass", 1),
    "kg": ("mass", 1000), "kilo": ("mass", 1000), "kilos": ("mass", 1000),
    "kilogram": ("mass", 1000), "kilograms": ("mass", 1000),
    # volume → millilitres
    "ml": ("volume", 1), "cl": ("volume", 10), "dl": ("volume", 100),
    "l": ("volume", 1000), "liter": ("volume", 1000), "litre": ("volume", 1000),
    "liters": ("volume", 1000), "litres": ("volume", 1000),
    # spoons/cups → teaspoons (3 tsp = 1 tbsp, 16 tbsp = 1 cup)
    "tsp": ("spoon", 1), "teaspoon": ("spoon", 1), "teaspoons": ("spoon", 1),
    "tbsp": ("spoon", 3), "tbs": ("spoon", 3), "tablespoon": ("spoon", 3),
    "tablespoons": ("spoon", 3), "cup": ("spoon", 48), "cups": ("spoon", 48),
}

# size/count words: not convertible, but summable as a count when the SAME word
# is used; mapped to a canonical singular token kept as a display suffix.
_COUNT_WORDS = {
    "small": "small", "medium": "medium", "large": "large",
    "clove": "clove", "cloves": "clove", "piece": "piece", "pieces": "piece",
    "slice": "slice", "slices": "slice", "can": "can", "cans": "can",
    "pack": "pack", "packs": "pack", "packet": "pack", "packets": "pack",
    "bunch": "bunch", "bunches": "bunch", "head": "head", "heads": "head",
    "stalk": "stalk", "stalks": "stalk", "sprig": "sprig", "sprigs": "sprig",
    "handful": "handful", "handfuls": "handful",
}

_NUM = r"\d+(?:[.,]\d+)?"


def _num(tok: str) -> float:
    return float(tok.replace(",", "."))


def _parse_amount(amount: str):
    """(lo, hi) numeric bounds for an amount string, or (None, None) when it is
    not a recognizable number / fraction / mixed number / range."""
    s = (amount or "").strip()
    if not s:
        return None, None
    m = re.fullmatch(rf"({_NUM})\s*[-–—]\s*({_NUM})", s)   # range "1-2" / "1–2"
    if m:
        return _num(m.group(1)), _num(m.group(2))
    m = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", s)             # mixed "1 1/2"
    if m:
        v = int(m.group(1)) + int(m.group(2)) / int(m.group(3))
        return v, v
    m = re.fullmatch(r"(\d+)/(\d+)", s)                    # fraction "1/2"
    if m:
        v = int(m.group(1)) / int(m.group(2))
        return v, v
    if re.fullmatch(_NUM, s):                              # plain "3" / "2.5" / "2,5"
        v = _num(s)
        return v, v
    return None, None


def parse_qty(amount: str, unit: str) -> dict:
    """Normalize amount+unit so quantities can be summed across days (F1).

    Returns {lo, hi, family, unit}:
      - lo/hi: lower/upper bound in the family's BASE unit (grams / ml / tsp /
        count); equal when not a range; None when the amount is unparseable.
      - family: 'mass' | 'volume' | 'spoon' | 'count' | '' (not summable).
      - unit: canonical display token — for 'count' the size word ('' for a bare
        number); '' otherwise (the base unit is implied by family).
    The raw amount/unit stay on the ingredient for display fallback."""
    lo, hi = _parse_amount(amount)
    u = (unit or "").strip().lower().rstrip(".")
    conv = _UNIT_TABLE.get(u)
    if conv:
        fam, factor = conv
        if lo is None:
            return {"lo": None, "hi": None, "family": "", "unit": ""}
        return {"lo": lo * factor, "hi": hi * factor, "family": fam, "unit": ""}
    word = _COUNT_WORDS.get(u)
    if (word is not None or u == "") and lo is not None:
        return {"lo": lo, "hi": hi, "family": "count", "unit": word or ""}
    return {"lo": None, "hi": None, "family": "", "unit": ""}

_CATEGORIZE_SYSTEM = (
    "You sort grocery ingredients into supermarket aisles so a shopping list groups "
    "similar products together the way a store is laid out. You are given a JSON array "
    "of ingredient names. Assign each to EXACTLY ONE aisle from this list: "
    + ", ".join(SHOP_CATEGORIES) + ". "
    "Guidance: 'International' = world-foods aisle staples like soy/fish/oyster sauce, "
    "curry paste, miso, tortillas' salsa, rice paper; 'Household' = non-food (foil, "
    "cleaning, napkins); 'Drinks' = juice, soda, water, wine, beer. Fresh fruit/veg/herbs "
    "go to 'Produce'. Use 'Other' only when nothing fits. "
    "Return ONLY a JSON object mapping each given name to its aisle, no markdown."
)


async def _ai_categorize_items(items: list) -> dict:
    """Ask Claude to sort ingredient names into SHOP_CATEGORIES. Returns a map
    {item: category}; anything missing/invalid falls back to the keyword matcher.
    Returns {} (all-fallback at the call site) when no API key is set."""
    uniq = list(dict.fromkeys(i.strip() for i in items if i and i.strip()))
    if not uniq:
        return {}
    allowed = set(SHOP_CATEGORIES)
    raw = await ai.complete_or_none(
        _CATEGORIZE_SYSTEM, [{"role": "user", "content": json.dumps(uniq)}], 2000,
        action="shopping.categorize")
    got = {}
    if raw:
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            got = json.loads(clean)
        except Exception:
            got = {}
        if not isinstance(got, dict):
            got = {}
    # Validate against the enum; fill any gap with the keyword matcher.
    return {i: (got.get(i) if got.get(i) in allowed else _ingredient_category(i)) for i in uniq}


async def _ensure_recipe_categories(recipe_ids: list):
    """Tag each recipe's ingredients with a store aisle (persisted on the recipe),
    so shopping-list grouping is accurate. Only recipes with untagged ingredients
    trigger an AI call; failures are swallowed so saving a plan never breaks.
    The AI call happens WITHOUT the write lock; the recipe is re-read inside the
    lock before applying, so a concurrent edit during the call is never lost."""
    from storage import write_recipe_file   # local import: keeps module surface tidy
    allowed = set(SHOP_CATEGORIES)

    def _untagged(recipe: dict) -> list:
        return [ing for ing in (recipe.get("ingredients", []) or [])
                if isinstance(ing, dict) and (ing.get("item") or "").strip()
                and ing.get("category") not in allowed]

    for rid in dict.fromkeys(rid for rid in recipe_ids if rid):
        try:
            recipe = read_recipe_file(rid)
            if not recipe:
                continue
            pending = _untagged(recipe)
            if not pending:
                continue
            cat_map = await _ai_categorize_items([ing["item"] for ing in pending])
            async with _write_lock:
                recipe = read_recipe_file(rid)   # re-read: may have changed during the AI call
                if not recipe:
                    continue
                for ing in _untagged(recipe):
                    item = ing["item"].strip()
                    ing["category"] = cat_map.get(item) or _ingredient_category(item)
                write_recipe_file(rid, recipe)   # category isn't an index field, so index is untouched
        except Exception:
            continue


def _day_ingredients(item: dict) -> list:
    """The aisle-tagged, qty-normalized ingredient rows for one planned meal
    (a `{id, name}` plan entry). Empty when the recipe is missing/ingredient-less."""
    recipe = read_recipe_file(item["id"]) if item.get("id") else None
    ings = []
    for ing in (recipe.get("ingredients", []) if recipe else []):
        it = (ing.get("item") or "").strip()
        if not it:
            continue
        amount = ing.get("amount", "")
        unit   = ing.get("unit", "")
        ings.append({
            "item":     it,
            "amount":   amount,
            "unit":     unit,
            "notes":    ing.get("notes", ""),
            "category": ing.get("category") or _ingredient_category(it),
            "qty":      parse_qty(amount, unit),   # F1: normalized for cumulative sums
        })
    return ings


def _extras_payload(state: dict) -> list:
    """F9 extras for a week's state, trimmed + aisle-tagged; blanks dropped."""
    return [{"item": it, "who": (e.get("who") or ""), "category": _ingredient_category(it)}
            for e in (state.get("extras") or []) if isinstance(e, dict)
            for it in [(e.get("item") or "").strip()] if it]


def _planned_meal(item) -> str:
    """The dish name of a plan entry, or '' when the day is empty/skipped/leftover."""
    if not isinstance(item, dict):
        return ""
    name = item.get("name", "")
    return "" if (not name or name.lower().startswith("leftover")) else name


def _shopping_payload(week_key: str) -> dict:
    """A fixed ISO week's ingredients (per recipe/day) with an aisle category for each,
    plus the saved 'have at home' / 'bought' / extras state. Backs the home
    `GET /shopping/{wk}` view (Mon–Sun); the phone relay uses the rolling variant."""
    plan   = read_meals().get("plan", {}).get(week_key, [])
    state  = read_shopping().get(week_key, {})
    days   = []
    for i, item in enumerate(plan):
        name = _planned_meal(item)
        if not name:
            continue   # skipped/leftover/empty days add no shopping
        days.append({"day": _SHOP_DAYS[i] if i < 7 else "", "name": name,
                     "id": item.get("id"), "ingredients": _day_ingredients(item)})
    return {"weekKey": week_key, "days": days, "extras": _extras_payload(state),
            "have": state.get("have", []), "bought": state.get("bought", [])}


def _rolling_shopping_payload(start: date | None = None, span: int = 6) -> dict:
    """The rolling shopping window for the phone relay: `span` days from `start`
    (default today), crossing ISO-week boundaries so the store list is always the
    days ahead, not a fixed Mon–Sun. Each day carries its ISO date. 'have' and
    'extras' are merged across every ISO week the window touches."""
    start  = start or date.today()
    plans  = read_meals().get("plan", {})
    shop   = read_shopping()
    days, weeks_touched = [], []
    for offset in range(span):
        d  = start + timedelta(days=offset)
        wk = _week_key_of(d)
        if wk not in weeks_touched:
            weeks_touched.append(wk)
        plan = plans.get(wk, [])
        wd   = d.weekday()   # Mon=0 … Sun=6, matching the stored plan order
        item = plan[wd] if wd < len(plan) else None
        name = _planned_meal(item)
        if not name:
            continue
        days.append({"day": _SHOP_DAYS[wd], "date": d.isoformat(), "name": name,
                     "id": item.get("id"), "ingredients": _day_ingredients(item)})
    have, extras, seen = [], [], set()
    for wk in weeks_touched:
        st = shop.get(wk, {})
        for h in st.get("have", []) or []:
            if h not in have:
                have.append(h)
        for e in _extras_payload(st):
            if e["item"].lower() not in seen:
                seen.add(e["item"].lower())
                extras.append(e)
    return {"weekKey": _week_key_of(start), "start": start.isoformat(),
            "days": days, "extras": extras, "have": have, "bought": []}


def _apply_extra(week_key: str, item: str, who: str = "", action: str = "add") -> bool:
    """Add or remove a manual shopping extra (F9) in shopping.json for a week.
    Dedupes by normalized name; 'add' replaces any existing same-name entry.
    Caller MUST hold `_write_lock`. Returns False for an empty item name."""
    item = (item or "").strip()
    if not item:
        return False
    norm = item.lower()
    data  = read_shopping()
    entry = data.get(week_key) or {}
    extras = [e for e in (entry.get("extras") or [])
              if isinstance(e, dict) and (e.get("item") or "").strip().lower() != norm]
    if action != "remove":
        extras.append({"item": item, "who": (who or "").strip()})
    entry["extras"] = extras
    data[week_key] = entry
    write_shopping(data)
    return True


def _week_key_of(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-{str(iso[1]).zfill(2)}"


def _current_week_key() -> str:
    return _week_key_of(date.today())


async def republish_shopping() -> bool:
    """Re-push the rolling window (always anchored on today) so the relay self-heals
    between explicit publishes. Called by the background sync loop; a no-op when the
    relay isn't configured."""
    return await publish_shopping()


async def publish_shopping(week_key: str | None = None) -> bool:
    """Push the rolling 6-day window (from today) to the cloud relay (outbound only).
    The `week_key` argument is accepted for call-site compatibility but ignored — the
    phone always shows the days ahead, not a fixed ISO week. No-op when the relay
    isn't configured; failures are swallowed so home actions never break."""
    if not relay_client._relay_ready():
        return False
    payload = _rolling_shopping_payload()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{relay_client.SHOP_RELAY_URL}/publish",
                headers=_relay_headers(),
                json={"week": payload["weekKey"], "start": payload["start"],
                      "days": payload["days"], "have": payload["have"],
                      "extras": payload["extras"]},
            )
        ok = resp.status_code == 200
        _log_relay("relay.publish_shopping", ok,
                   None if ok else {"status": resp.status_code, "url": relay_client.SHOP_RELAY_URL})
        return ok
    except Exception as e:
        _log_relay("relay.publish_shopping", False, {"error": str(e), "url": relay_client.SHOP_RELAY_URL})
        return False


@router.get("/shopping/{week_key}")
def get_shopping(week_key: str):
    """Ingredients + state for a week's plan; the client builds the two lists."""
    return _shopping_payload(week_key)


@router.post("/shopping/{week_key}")
async def save_shopping(week_key: str, request: Request):
    body = await request.json()   # {have: [...], bought: [...]}
    async with _write_lock:
        data = read_shopping()
        entry = data.get(week_key) or {}
        entry["have"]   = body.get("have", [])
        entry["bought"] = body.get("bought", [])
        data[week_key]  = entry   # keep 'extras' (F9) untouched
        write_shopping(data)
    await publish_shopping(week_key)   # keep the store's copy in sync with at-home edits
    await broadcast("update", {"section": "shopping"})
    return {"ok": True}


@router.post("/shopping/{week_key}/extra")
async def add_shopping_extra(week_key: str, request: Request):
    """Add a manual shopping item (F9) for the week — e.g. 'batteries, dish soap'.
    Used by mobile.html at home; the phone queues a `shopping_extra` relay command
    that lands here via _apply_extra. Body: {item, who?}."""
    body = await request.json()
    async with _write_lock:
        ok = _apply_extra(week_key, body.get("item", ""), body.get("who", ""))
    if not ok:
        raise HTTPException(400, "Empty item")
    log_event("data", "shopping.extra", f"Added shopping extra to {week_key}",
              who=body.get("who", ""), detail={"item": (body.get("item") or "").strip()})
    await publish_shopping(week_key)
    await broadcast("update", {"section": "shopping"})
    return {"ok": True}


@router.post("/shopping/{week_key}/extra/delete")
async def delete_shopping_extra(week_key: str, request: Request):
    """Remove a manual shopping extra (F9). Body: {item}."""
    body = await request.json()
    async with _write_lock:
        _apply_extra(week_key, body.get("item", ""), action="remove")
    await publish_shopping(week_key)
    await broadcast("update", {"section": "shopping"})
    return {"ok": True}


def _aggregate_for_pricing(payload: dict) -> list:
    """One row per distinct ingredient for the whole week, quantities summed.

    The clients sum per-day quantities themselves for display (see parse_qty), but
    a price estimate has to do it server-side: buying 200 g of mince on Tuesday and
    300 g on Friday is one 500 g purchase, and pricing the days separately would
    round a pack in twice. Items already marked 'have at home' are excluded — you
    aren't buying those. Quantities only sum within one family; a mix (400 g mince
    and '2 packs' mince) keeps the mass and drops the odd one out rather than
    inventing a total.
    """
    have = {h.lower() for h in (payload.get("have") or [])}
    rows: dict[str, dict] = {}
    for day in payload.get("days", []):
        for ing in day.get("ingredients", []):
            name = (ing.get("item") or "").strip()
            if not name or name.lower() in have:
                continue
            qty = ing.get("qty") or {}
            row = rows.get(name.lower())
            if row is None:                    # first sighting seeds the quantity;
                rows[name.lower()] = {"item": name, "qty": dict(qty)}
                continue                       # adding it here too would count it twice
            cur = row["qty"]
            if cur.get("family") and cur.get("family") == qty.get("family") \
                    and cur.get("lo") is not None and qty.get("lo") is not None:
                cur["lo"] += qty["lo"]
                cur["hi"] = (cur.get("hi") or cur["lo"]) + (qty.get("hi") or qty["lo"])
    for e in payload.get("extras", []):
        name = (e.get("item") or "").strip()
        if name and name.lower() not in have:
            rows.setdefault(name.lower(), {"item": name, "qty": {}})
    return list(rows.values())


@router.get("/shopping/{week_key}/price")
async def price_shopping(week_key: str):
    """Estimate this week's list at Willys (read-only, anonymous — see willys.py).

    Never fails the request: a store-side problem comes back as `error` with an
    empty total, because an estimate is a convenience and the shopping page must
    render without it.
    """
    items = _aggregate_for_pricing(_shopping_payload(week_key))
    return await willys.estimate(items)


@router.post("/shopping/{week_key}/publish")
async def publish_shopping_route(week_key: str):
    """Manually refresh the phone's rolling shopping list ('Publish to phone').
    The window always anchors on today, so the week argument is informational."""
    if not relay_client.SHOP_RELAY_URL:
        raise HTTPException(400, "Shopping relay not configured (set SHOP_RELAY_URL)")
    ok = await publish_shopping()
    if not ok:
        log_event("cloud", "relay.publish_shopping", "Manual 'Publish to phone' failed", level="error",
                  detail={"week": week_key})
        raise HTTPException(502, "Could not reach the shopping relay")
    log_event("cloud", "relay.publish_shopping", "Published shopping list to phone")
    return {"ok": True}
