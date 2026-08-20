"""Display-cache builder + recurrence engine. The pre-rendered device payload
(the "materialized view" at the heart of the system) is rebuilt after every
mutation and written to cache/display.json — the Inky Frame fetches it with
zero computation at request time."""
import unicodedata
from datetime import date, timedelta

from activity_log import log_event
from storage import (_BYDAY, F_DISPLAY, read_events, read_meals, read_recipe_file,
                     read_settings, write)
from swedish_holidays import swedish_holidays

# ── ASCII folding for the Inky Frame ──────────────────────────────────────────
# The device draws with PicoGraphics' `bitmap8`, which only has glyphs for ASCII
# 32–126. Anything above that renders as garbage or nothing — which covers half
# the recipe library (Turkish: ı ş ğ ç ö ü) and the Swedish red days (å ä ö).
# Adding glyphs would mean rebuilding the device firmware, so instead the payload
# is folded to ASCII *at serve time*, and only when the device asks for it
# (`/display-data?ascii=1`). The stored cache, the web UI and the phone all keep
# proper Unicode — only the e-ink screen sees the folded text.
#
# NFKD alone is not enough: `ı` (dotless i) and `İ` have no decomposition, so they
# would be dropped silently rather than folded. Map those explicitly first.
_FOLD_PRE = str.maketrans({
    "ı": "i", "İ": "I",   # no Unicode decomposition — must be handled by hand
    "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE",
    "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...",
})

def fold_ascii(obj):
    """Recursively fold text to ASCII. Strings only; structure is preserved."""
    if isinstance(obj, str):
        s = obj.translate(_FOLD_PRE)
        # Decompose (ş → s + cedilla) and drop the combining marks.
        s = "".join(c for c in unicodedata.normalize("NFKD", s)
                    if not unicodedata.combining(c))
        # Anything still non-ASCII has no sensible fold — drop it rather than
        # hand the device a byte it will draw as a random glyph.
        return s.encode("ascii", "ignore").decode("ascii")
    if isinstance(obj, dict):
        return {k: fold_ascii(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fold_ascii(v) for v in obj]
    return obj

# ── Recurrence engine (F2) ─────────────────────────────────────────────────────
def _recurring_occurrences(r: dict, win_start: date, win_end: date) -> list:
    """Concrete occurrence dates for a recurring item within [win_start, win_end].
    Supports the legacy form (step in days) and the v2 form
    {freq: "daily"|"weekly", interval, byday, until, exdates}; `until` (inclusive)
    and `exdates` apply to both. Jump-ahead keeps this O(window) even for a
    years-old startDate."""
    start = date.fromisoformat(str(r["startDate"]))
    until = win_end
    if r.get("until"):
        until = min(until, date.fromisoformat(str(r["until"])))
    lo = max(start, win_start)
    if lo > until:
        return []
    exdates = {str(x) for x in (r.get("exdates") or [])}
    out = []
    if r.get("freq") == "weekly":
        interval = max(1, int(r.get("interval") or 1))
        days = sorted(_BYDAY[b] for b in (r.get("byday") or []) if b in _BYDAY) or [start.weekday()]
        week0 = start - timedelta(days=start.weekday())     # Monday of the start week
        wk = (lo - timedelta(days=lo.weekday()) - week0).days // 7
        w = week0 + timedelta(weeks=wk - wk % interval)     # last aligned week ≤ lo's week
        while w <= until:
            for dow in days:
                d = w + timedelta(days=dow)
                if lo <= d <= until and d >= start and str(d) not in exdates:
                    out.append(d)
            w += timedelta(weeks=interval)
    else:
        # v2 daily, or legacy step-in-days (≡ daily with interval=step)
        interval = max(1, int(r.get("interval") or 1) if r.get("freq") == "daily" else int(r["step"]))
        k = max(0, -((start - lo).days // interval))        # ceil((lo-start)/interval)
        d = start + timedelta(days=k * interval)
        while d <= until:
            if str(d) not in exdates:
                out.append(d)
            d += timedelta(days=interval)
    return out

# ── Display cache builder ─────────────────────────────────────────────────────
# Malformed entries are skipped, not fatal — but each is warned once (this
# builder runs on every mutation, so unbounded repeats would flood the log).
_cache_warned: set = set()

# ASCII-only, and short enough to sit beside a day number in a 113px cell. The Inky's
# bitmap8 font has no glyph above 126, so nothing here may grow an accent.
_MONTH_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _window_title(start: date, end: date) -> str:
    """Name the rolling window: "August 2026" inside one month, "Aug - Sep 2026"
    across two, "Dec 2026 - Jan 2027" across a year end. Plain hyphen, never an en
    dash — the device font would drop it and fold_ascii would have to rescue it."""
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.strftime('%B')} {start.year}"
        return f"{_MONTH_SHORT[start.month - 1]} - {_MONTH_SHORT[end.month - 1]} {start.year}"
    return (f"{_MONTH_SHORT[start.month - 1]} {start.year} - "
            f"{_MONTH_SHORT[end.month - 1]} {end.year}")

def build_display_cache(week_offset: int = 0) -> dict:
    """
    Pre-render the full Pico payload and write to cache/display.json.
    Called after any data change. Pico fetches this directly — zero computation at fetch time.

    The grid is a ROLLING 4-week window, not a calendar month: row 0 is always the
    Monday-start week containing `today`, and the three weeks after it fill the rest.
    Today's marker therefore travels left-to-right across the top row and never
    descends — when a week ends the whole window slides up. A month grid put today in
    the bottom row by month's end, leaving almost no forward visibility exactly when
    it was most wanted. `week_offset` slides the window in whole weeks (the device's
    buttons use ±4).
    """
    settings = read_settings()
    ev_data  = read_events()
    meals    = read_meals()
    today    = date.today()
    window_start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    window_end   = window_start + timedelta(days=27)
    # Kept for anything still reading the old month-shaped fields; they describe the
    # day the window opens on, which is all a month name can honestly mean here.
    year     = window_start.year
    month    = window_start.month

    # Expand event map
    event_map: dict[str, list] = {}
    def add(key, ev): event_map.setdefault(key, []).append(ev)

    members = {m["id"]: m for m in settings.get("members", [])}

    for b in ev_data.get("birthdays", []):
        for y in sorted({window_start.year, window_end.year}):
            try:
                d = date(y, b["month"], b["day"])
                label = b["name"]
                born  = b.get("year")
                if isinstance(born, int) and 1900 <= born <= y:
                    label = f"{b['name']} ({y - born})"   # show the age they turn
                add(str(d), {"type": "birthday", "label": label, "icon": "cake", "icon_only": False})
            except (ValueError, KeyError, TypeError) as e:
                key = ("birthday", str(b.get("name", "")))
                if key not in _cache_warned:
                    _cache_warned.add(key)
                    log_event("data", "cache.skip_birthday",
                              f"Skipping malformed birthday '{b.get('name', '')}': {e}", level="warn")

    # Grid bounds: recurring items, multi-day spans and holidays are expanded only
    # across days the payload can actually render — i.e. the rolling window itself.
    grid_first = window_start
    grid_last  = window_end

    for r in ev_data.get("recurring", []):
        try:
            owner = members.get(r.get("who", "family"), members.get("family", {}))
            t = r.get("time") or ""
            entry = {
                "type": "recurring",
                "label": r["label"],
                "time": t,
                "icon": r["icon"],
                "icon_only": True,
                "bg": owner.get("bg", "#000000"),
                "text_color": owner.get("text", "#ffffff"),
            }
            for d in _recurring_occurrences(r, grid_first, grid_last):
                add(str(d), entry)
        except Exception as e:
            key = ("recurring", str(r.get("label", "")))
            if key not in _cache_warned:
                _cache_warned.add(key)
                log_event("data", "cache.skip_recurring",
                          f"Skipping malformed recurring '{r.get('label', '')}': {e}", level="warn")

    for e in ev_data.get("events", []):
        try:
            m = members.get(e["who"], {})
            t = e.get("time") or ""
            entry = {
                "type": "personal", "who": e["who"],
                # Label is the event name only — the display surfaces (Inky +
                # display.html) don't show the time. The raw time rides along in
                # its own field purely to drive the within-day sort order.
                "label": e["label"],
                "time": t,
                "icon": e.get("icon", ""),
                "icon_only": False,
                "bg": m.get("bg", "#000000"),
                "text_color": m.get("text", "#ffffff"),
            }
            # Multi-day: endDate (inclusive) expands onto every day, capped at 60.
            d0 = date.fromisoformat(e["date"])
            d1 = date.fromisoformat(e["endDate"]) if e.get("endDate") else d0
            if d1 < d0:
                d1 = d0
            d1 = min(d1, d0 + timedelta(days=59))
            cur = d0
            while cur <= d1:
                add(str(cur), entry)
                cur += timedelta(days=1)
        except Exception as ex:
            key = ("event", str(e.get("label", "")))
            if key not in _cache_warned:
                _cache_warned.add(key)
                log_event("data", "cache.skip_event",
                          f"Skipping malformed event '{e.get('label', '')}': {ex}", level="warn")

    # Swedish red days (F5) — computed locally, gated on the display toggle.
    display_cfg   = settings.get("display", {})
    show_holidays = display_cfg.get("showHolidays", True)
    holiday_dates: dict[str, str] = {}
    if show_holidays:
        for yr in range(grid_first.year, grid_last.year + 1):
            holiday_dates.update(swedish_holidays(yr))
        for dstr, name in holiday_dates.items():
            d = date.fromisoformat(dstr)
            if grid_first <= d <= grid_last:
                add(dstr, {"type": "holiday", "label": name, "icon_only": False})

    # Within a day: birthdays first, then holidays, then everything else in
    # scheduled order (untimed items sort ahead of timed ones — "" < any "HH:MM").
    _TYPE_ORDER = {"birthday": 0, "holiday": 1}
    for evs in event_map.values():
        evs.sort(key=lambda x: (_TYPE_ORDER.get(x.get("type"), 2), x.get("time") or ""))

    # Calendar grid — 28 cells, 4 rows, always starting on a Monday. There is no
    # off-month concept any more: the window spans two months most weeks, so shading
    # "the other month" would grey out half the screen. Instead the 1st of a month
    # carries a `month_short`, which the renderers draw as "1 Sep" beside the number.
    cells = []
    for i in range(28):
        d = window_start + timedelta(days=i)
        cell = {"date": str(d), "day": d.day, "today": d == today,
                "events": event_map.get(str(d), [])}
        if d.day == 1:
            cell["month_short"] = _MONTH_SHORT[d.month - 1]
        cells.append(cell)

    # Flag holiday cells so the renderers can paint the day number red.
    for cell in cells:
        cell["holiday"] = cell["date"] in holiday_dates

    # Current week meals (+ next week, so a Sunday "tomorrow" view can show it)
    monday   = today - timedelta(days=today.weekday())
    iso      = monday.isocalendar()
    week_key = f"{iso[0]}-{str(iso[1]).zfill(2)}"
    meal_plan = meals.get("plan", {}).get(week_key, [])
    niso           = (monday + timedelta(days=7)).isocalendar()
    next_week_key  = f"{niso[0]}-{str(niso[1]).zfill(2)}"
    meal_plan_next = meals.get("plan", {}).get(next_week_key, [])

    # Recipe detail for a given date (right-hand pane of the meals screens).
    # Looks up that date's own ISO week so tomorrow works even across a week boundary.
    def _fmt_ing(i):
        s = " ".join(str(i.get(k, "")).strip() for k in ("amount", "unit", "item") if str(i.get(k, "")).strip())
        n = str(i.get("notes", "")).strip()
        return s + (f" ({n})" if n else "")

    def _meal_detail(d):
        di   = d.isocalendar()
        wk   = f"{di[0]}-{str(di[1]).zfill(2)}"
        plan = meals.get("plan", {}).get(wk, [])
        idx  = d.weekday()
        if idx >= len(plan) or not isinstance(plan[idx], dict):
            return None
        item = plan[idx]
        if not item.get("name"):
            return None
        detail = {"day_index": idx, "name": item.get("name", ""), "notes": item.get("notes", "")}
        recipe = read_recipe_file(item["id"]) if item.get("id") else None
        if recipe and not item.get("name", "").lower().startswith("leftover"):
            detail.update({
                "cuisine":       recipe.get("cuisine", ""),
                "prep_time_min": recipe.get("prep_time_min", 0),
                "cook_time_min": recipe.get("cook_time_min", 0),
                "difficulty":    recipe.get("difficulty", 0),
                "servings":      recipe.get("servings", 0),
                "ingredients":   [_fmt_ing(i) for i in recipe.get("ingredients", [])],
                "steps":         [str(s.get("text", "")).strip() for s in recipe.get("steps", [])],
            })
        return detail

    today_meal    = _meal_detail(today)
    tomorrow_meal = _meal_detail(today + timedelta(days=1))

    event_colors = dict(settings.get("eventColors", {}))
    event_colors.setdefault("birthday", "#ffff00")
    event_colors.setdefault("recurring", "#000000")
    event_colors.setdefault("holiday", "#ff0000")
    event_colors.setdefault("birthdayText", "#000000")   # F5: configurable text colors
    event_colors.setdefault("holidayText", "#ffffff")
    legend = [{"label": m["label"], "color": m.get("bg", "#000000"), "text_color": m.get("text", "#ffffff")} for m in settings.get("members", [])]
    legend.append({"label": "Birthday",  "color": event_colors["birthday"], "text_color": event_colors["birthdayText"]})
    if show_holidays:
        legend.append({"label": "Holiday", "color": event_colors["holiday"], "text_color": event_colors["holidayText"]})

    payload = {
        "month": month, "year": year,
        "month_name": date(year, month, 1).strftime("%B"),
        "title": _window_title(window_start, window_end),
        "window_start": str(window_start),
        "window_end": str(window_end),
        "today": str(today),
        "cells": cells,
        "meal_plan": meal_plan,
        "meal_plan_next": meal_plan_next,
        "today_meal": today_meal,
        "tomorrow_meal": tomorrow_meal,
        "legend": legend,
        "family_name": settings.get("familyName", ""),
        "app_name": settings.get("appName", "Family Calendar"),
        "event_colors": event_colors,
        "generated_at": today.isoformat(),
    }
    # Only the window anchored on today is the canonical cached view; ±offset windows
    # are computed on demand and must NOT overwrite the cache (that caused Home to
    # show a previously-viewed grid).
    if week_offset == 0:
        write(F_DISPLAY, payload)
    return payload
