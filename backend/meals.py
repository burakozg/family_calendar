"""Meal planning: the weekly plan store + the two-step AI planner
(generate from the standing prompt + recipe library, then refine on request)."""
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

import ai
from activity_log import log_event, parse_ai_json
from bus import broadcast
from display_cache import build_display_cache
from shopping import _ensure_recipe_categories, publish_shopping
from storage import (DEFAULT_INITIAL_PROMPT, DEFAULT_SYS_GENERATE,
                     DEFAULT_SYS_REFINE, F_MEALS, _write_lock, read_meals,
                     read_recipe_index, read_settings, write)

router = APIRouter()


@router.get("/meals")
def get_meals(): return read_meals()


@router.patch("/meals/plan")
async def patch_meal_plan(request: Request):
    body = await request.json()   # {weekKey, meals}
    async with _write_lock:
        m = read_meals()
        m.setdefault("plan", {})[body["weekKey"]] = body["meals"]
        write(F_MEALS, m)
    # Tag the chosen recipes' ingredients with store aisles for accurate shopping-list
    # grouping (persisted on the recipe; only untagged recipes hit the AI). Non-fatal.
    # Outside the lock: it makes AI calls and takes the lock itself per recipe.
    try:
        await _ensure_recipe_categories([
            item.get("id") for item in (body.get("meals") or []) if isinstance(item, dict)
        ])
    except Exception:
        pass
    async with _write_lock:
        build_display_cache()
    await publish_shopping(body["weekKey"])   # keep the store's copy current (no-op if relay unset)
    await broadcast("update", {"section": "meals"})
    dinners = sum(1 for m in (body.get("meals") or []) if isinstance(m, dict) and m.get("name"))
    log_event("data", "meals.save",
              f"Saved meal plan for {body['weekKey']} ({dinners} dinners)",
              detail={"week": body["weekKey"]})
    return {"ok": True}


# ── AI planner ────────────────────────────────────────────────────────────────
def _recipe_option_line(e: dict) -> str:
    """One compact candidate line for the meal-planning assistant. Tags are
    stringified defensively: a single recipe with odd tags (an import once saved
    them as {value, source} dicts) must not take the whole planner down."""
    raw   = e.get("tags")
    tags  = ", ".join(t for t in raw if isinstance(t, str)) if isinstance(raw, list) else str(raw or "")
    total = (e.get("prep_time_min", 0) or 0) + (e.get("cook_time_min", 0) or 0)
    bits  = [e.get("cuisine", ""), tags]
    if total: bits.append(f"{total} min")
    if e.get("creates_leftovers"): bits.append("makes leftovers")
    if e.get("source_type") == "ai": bits.append("AI")
    meta  = " · ".join(b for b in bits if b)
    return f'- [{e.get("id","")}] {e.get("name","")}' + (f" — {meta}" if meta else "")


TWO_WEEK_NEED = 14   # dinners needed for the current + next week

# Planner history windows — defaults; overridable per-household in settings.mealPlanner.
RECENT_WEEKS = 2     # avoid repeating a dish planned within this many weeks
STALE_WEEKS  = 6     # a dish unseen this long counts as "not cooked in a while"

def _clamp_weeks(val, default: int, lo: int, hi: int) -> int:
    """Coerce a settings value to an int within [lo, hi]; fall back on garbage."""
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default

def _planner_options(include_ai: bool):
    """Recipe options for the planner. The family's own recipes are the default pool;
    AI-sourced recipes are offered only as a fallback (too few human recipes to fill
    two weeks) or when the user is explicitly directing an edit (refine)."""
    index = read_recipe_index()
    human   = [e for e in index if e.get("source_type") != "ai"]
    ai_recs = [e for e in index if e.get("source_type") == "ai"]
    if include_ai or len(human) < TWO_WEEK_NEED:
        return human + ai_recs
    return human


DAYS7 = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

def _meal_prompts(settings: dict) -> dict:
    """Effective meal-planner prompts: settings values, falling back to defaults."""
    mp = settings.get("mealPlanner", {}) or {}
    return {
        "initialPrompt":  mp.get("initialPrompt")  or settings.get("mealPrompt") or DEFAULT_INITIAL_PROMPT,
        "systemGenerate": mp.get("systemGenerate") or DEFAULT_SYS_GENERATE,
        "systemRefine":   mp.get("systemRefine")   or DEFAULT_SYS_REFINE,
        "recentWeeks":    _clamp_weeks(mp.get("recentWeeks"), RECENT_WEEKS, 0, 12),
        "staleWeeks":     _clamp_weeks(mp.get("staleWeeks"),  STALE_WEEKS, 1, 52),
    }


def _plan_lines(plan: list) -> str:
    """Render the current 7-day plan as text for the refine prompt."""
    out = []
    for i in range(7):
        d = plan[i] if i < len(plan) and isinstance(plan[i], dict) else {}
        name = d.get("name") or "(empty)"
        line = f"{DAYS7[i]}: {name}"
        if d.get("id"):    line += f" [{d['id']}]"
        if d.get("notes"): line += f" — {d['notes']}"
        out.append(line)
    return "\n".join(out)


async def _meal_llm(system: str, user: str, action: str = "meal.plan") -> list:
    """Shared LLM call for meal planning; returns the parsed JSON plan."""
    raw = await ai.complete(system, [{"role": "user", "content": user}], 1500, action=action)
    return parse_ai_json(raw, action)


@router.get("/meals/prompts")
def get_meal_prompts():
    """Effective meal-planner prompts, for the Settings UI."""
    return _meal_prompts(read_settings())


def _recent_from_history(week_start: date, weeks_back: int = RECENT_WEEKS) -> list:
    """F7: recipe names served in the `weeks_back` weeks before `week_start`,
    read straight from the saved plan — history is derived server-side rather
    than trusted from the client. ISO-week keys match how the plan is stored
    and read everywhere else (see display_cache)."""
    week_start -= timedelta(days=week_start.weekday())   # snap to that week's Monday
    plan = read_meals().get("plan") or {}
    names, seen = [], set()
    for back in range(1, weeks_back + 1):
        iso = (week_start - timedelta(weeks=back)).isocalendar()
        for entry in plan.get(f"{iso[0]}-{str(iso[1]).zfill(2)}", []) or []:
            if isinstance(entry, dict):
                n = (entry.get("name") or "").strip()
                if n and n not in seen:
                    seen.add(n)
                    names.append(n)
    return names


def _long_time_no_cook(week_start: date, library_names: set,
                       stale_weeks: int = STALE_WEEKS, limit: int = 8) -> list:
    """F7 variety signal: library recipes last planned at least `stale_weeks`
    before `week_start` — "not cooked in a while", nudging the planner to bring
    favourites back. Derived from plan history alone (no cooked/skipped tracking).
    Never-planned recipes are already fresh options in the library, so they're
    not listed here; oldest-unseen first."""
    week_start -= timedelta(days=week_start.weekday())
    cutoff = week_start - timedelta(weeks=stale_weeks)
    plan = read_meals().get("plan") or {}
    last_seen: dict = {}
    for key, entries in plan.items():
        try:
            y, w = (int(x) for x in str(key).split("-"))
            monday = date.fromisocalendar(y, w, 1)
        except (ValueError, TypeError):
            continue
        if monday >= week_start:
            continue                        # ignore the planned week and the future
        for entry in entries or []:
            if isinstance(entry, dict):
                n = (entry.get("name") or "").strip()
                if n and monday > last_seen.get(n, date.min):
                    last_seen[n] = monday
    stale = [(n, d) for n, d in last_seen.items() if d <= cutoff and n in library_names]
    stale.sort(key=lambda nd: nd[1])        # oldest first
    return [n for n, _ in stale[:limit]]


@router.post("/meals/plan/generate")
async def generate_meal_plan(request: Request):
    """Step 1 — create the week's recommended plan from the standing prompt + recipe library."""
    ai.ensure_ready()
    body        = await request.json()   # {weekSummary, userPrompt?, weekStart?}
    prompts     = _meal_prompts(read_settings())
    library     = _planner_options(include_ai=False)   # AI recipes only as fallback when too few human recipes
    recipe_list = "\n".join(_recipe_option_line(e) for e in library) or "(no recipes saved yet)"
    # Repeat-avoidance history: read the plan server-side for the weeks before
    # the one being planned. The client sends that week's Monday; if it's missing
    # or malformed, fall back to the current week.
    try:
        week_start = date.fromisoformat(str(body.get("weekStart", "")))
    except ValueError:
        week_start = date.today()
    recent_weeks = prompts["recentWeeks"]
    stale_weeks  = prompts["staleWeeks"]
    recent      = _recent_from_history(week_start, recent_weeks)
    recent_txt  = "\n".join(f"- {n}" for n in recent) or "(nothing recorded)"
    library_names = {(e.get("name") or "").strip() for e in library if e.get("name")}
    stale       = _long_time_no_cook(week_start, library_names, stale_weeks)
    stale_txt   = "\n".join(f"- {n}" for n in stale) or "(none)"
    extra       = (body.get("userPrompt") or "").strip()
    user = (
        f"Recipe library (the only allowed options):\n{recipe_list}\n\n"
        f"Recently served in the last {recent_weeks} weeks — avoid repeating these:\n{recent_txt}\n\n"
        f"Not cooked in a while — nice to bring back for variety (optional, only if they fit):\n{stale_txt}\n\n"
        f"Standing preferences:\n{prompts['initialPrompt']}\n\n"
        f"This week, day by day (events are shown; a day may already have dinner covered):\n{body.get('weekSummary','')}"
        + (f"\n\nExtra request for this week:\n{extra}" if extra else "")
    )
    meals = await _meal_llm(prompts["systemGenerate"], user, action="meal.generate")
    log_event("ai", "meal.generate", f"Generated a week plan ({sum(1 for m in meals if m and m.get('name'))} dinners)",
              detail={"userPrompt": extra} if extra else None)
    return {"meals": meals}


@router.post("/meals/plan/refine")
async def refine_meal_plan(request: Request):
    """Step 2 — apply the user's change request to the current plan (a day or the whole week)."""
    ai.ensure_ready()
    body        = await request.json()   # {weekSummary, currentPlan, instruction}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "No instruction provided")
    prompts     = _meal_prompts(read_settings())
    library     = _planner_options(include_ai=True)    # user-directed: AI recipes available on request
    recipe_list = "\n".join(_recipe_option_line(e) for e in library) or "(no recipes saved yet)"
    user = (
        f"Recipe library (the only allowed options):\n{recipe_list}\n\n"
        "Recipes marked AI are fallback ideas — prefer the family's own recipes; use an AI one only if the request specifically asks for it or nothing else fits.\n\n"
        f"Current plan (Monday to Sunday):\n{_plan_lines(body.get('currentPlan', []))}\n\n"
        f"This week's context (busy days etc.):\n{body.get('weekSummary','')}\n\n"
        f"Change request:\n{instruction}"
    )
    meals = await _meal_llm(prompts["systemRefine"], user, action="meal.refine")
    log_event("ai", "meal.refine", "Refined the week plan", detail={"instruction": instruction})
    return {"meals": meals}


@router.post("/meals/suggest")
async def suggest_meals(request: Request):
    """Backward-compatible alias for the generate step."""
    return await generate_meal_plan(request)
