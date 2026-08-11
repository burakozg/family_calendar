"""F7: cooking history derived server-side — the planner reads the previous two
weeks straight from the saved plan instead of trusting a client `recent` list."""
from datetime import date, timedelta

import meals
import storage


def _wk(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-{str(iso[1]).zfill(2)}"


def test_recent_reads_prior_two_weeks_dedup_and_window():
    target = date(2026, 7, 13)                        # a Monday
    plan = {
        _wk(target - timedelta(weeks=1)): [{"name": "Tacos"}, {"name": "Pasta"}, {}, {"name": "Tacos"}],
        _wk(target - timedelta(weeks=2)): [{"name": "Curry"}],
        _wk(target - timedelta(weeks=3)): [{"name": "TooOld"}],   # outside the 2-week window
        _wk(target):                      [{"name": "ThisWeek"}], # the week being planned, excluded
    }
    storage.write(storage.F_MEALS, {"plan": plan, "recipes": []})
    recent = meals._recent_from_history(target)
    assert recent == ["Tacos", "Pasta", "Curry"]      # order preserved, deduped
    assert "TooOld" not in recent and "ThisWeek" not in recent


def test_recent_snaps_midweek_input_to_its_monday():
    target = date(2026, 7, 13)
    storage.write(storage.F_MEALS, {"plan": {
        _wk(target - timedelta(weeks=1)): [{"name": "Ramen"}]}, "recipes": []})
    # A Thursday in the same week must yield the same history as its Monday.
    assert meals._recent_from_history(target + timedelta(days=3)) == ["Ramen"]


def test_recent_empty_when_no_history():
    storage.write(storage.F_MEALS, {"plan": {}, "recipes": []})
    assert meals._recent_from_history(date(2026, 7, 13)) == []


def test_long_time_no_cook_signal():
    target = date(2026, 7, 13)
    plan = {
        _wk(target - timedelta(weeks=1)): [{"name": "Tacos"}],     # recent → not stale
        _wk(target - timedelta(weeks=4)): [{"name": "Chili"}],     # in-between → neither
        _wk(target - timedelta(weeks=6)): [{"name": "Lasagne"}],   # stale
        _wk(target - timedelta(weeks=9)): [{"name": "Ramen"}],     # stale, older
    }
    storage.write(storage.F_MEALS, {"plan": plan, "recipes": []})
    lib = {"Tacos", "Chili", "Lasagne", "Ramen", "NeverPlanned"}
    stale = meals._long_time_no_cook(target, lib)
    assert stale == ["Ramen", "Lasagne"]          # oldest-unseen first
    assert "Chili" not in stale                   # 4 weeks: neither recent nor stale
    assert "NeverPlanned" not in stale            # never cooked ≠ long-time-no-cook


def test_long_time_no_cook_restricted_to_library():
    target = date(2026, 7, 13)
    storage.write(storage.F_MEALS, {"plan": {
        _wk(target - timedelta(weeks=8)): [{"name": "Goulash"}, {"name": "Gone"}]}, "recipes": []})
    # "Gone" was cooked but is no longer in the library → not suggested.
    assert meals._long_time_no_cook(target, {"Goulash"}) == ["Goulash"]


def test_meal_prompts_expose_clamped_week_settings():
    # defaults come through
    p = meals._meal_prompts({"mealPlanner": {}})
    assert (p["recentWeeks"], p["staleWeeks"]) == (2, 6)
    # explicit values honoured; out-of-range clamped; garbage falls back
    p = meals._meal_prompts({"mealPlanner": {"recentWeeks": 3, "staleWeeks": 99}})
    assert (p["recentWeeks"], p["staleWeeks"]) == (3, 52)
    p = meals._meal_prompts({"mealPlanner": {"recentWeeks": "x", "staleWeeks": 0}})
    assert (p["recentWeeks"], p["staleWeeks"]) == (2, 1)   # bad→default, 0→clamped to min 1


def test_configurable_windows_change_history_results():
    target = date(2026, 7, 13)
    storage.write(storage.F_MEALS, {"plan": {
        _wk(target - timedelta(weeks=1)): [{"name": "Pho"}],
        _wk(target - timedelta(weeks=3)): [{"name": "Stew"}],
    }, "recipes": []})
    # A 1-week recent window sees only week-1; a 3-week window also sees Stew.
    assert meals._recent_from_history(target, 1) == ["Pho"]
    assert set(meals._recent_from_history(target, 3)) == {"Pho", "Stew"}
    # Stale threshold of 3 weeks makes the 3-week-old Stew a "bring back" pick.
    assert meals._long_time_no_cook(target, {"Stew", "Pho"}, stale_weeks=3) == ["Stew"]
