"""build_display_cache: event placement, birthday age labels, malformed-entry
skips (warned once), and the cache-write rule for shifted windows."""
import json
from datetime import date, timedelta

import main


def _set_events(doc):
    base = {"events": [], "birthdays": [], "recurring": []}
    base.update(doc)
    main.write(main.F_EVENTS, base)


def _cell_labels(payload):
    return [(c["date"], e["label"]) for c in payload["cells"] for e in c["events"]]


def test_event_lands_on_its_day():
    d = str(date.today())
    _set_events({"events": [{"date": d, "who": "family", "icon": "star", "label": "CacheTest"}]})
    payload = main.build_display_cache(0)
    assert (d, "CacheTest") in _cell_labels(payload)


def test_birthday_age_label():
    # A few days out, so it is inside the rolling window whatever today's date is.
    d = date.today() + timedelta(days=3)
    _set_events({"birthdays": [{"name": "Grandma", "month": d.month, "day": d.day, "year": 1950}]})
    payload = main.build_display_cache(0)
    labels = [l for _, l in _cell_labels(payload)]
    assert f"Grandma ({d.year - 1950})" in labels


def test_birthday_without_year_unchanged():
    d = date.today() + timedelta(days=3)
    _set_events({"birthdays": [{"name": "Uncle", "month": d.month, "day": d.day}]})
    labels = [l for _, l in _cell_labels(main.build_display_cache(0))]
    assert "Uncle" in labels


def test_malformed_recurring_skipped_and_warned_once():
    _set_events({"recurring": [{"label": "BadOne", "icon": "trash", "startDate": "not-a-date", "step": 7}]})
    main._cache_warned.clear()
    main.build_display_cache(0)
    main.build_display_cache(0)   # second run must not warn again
    entries = main.read_logs(q="BadOne")
    assert len(entries) == 1 and entries[0]["level"] == "warn"


def test_recurring_expands_by_step():
    start = date.today()
    _set_events({"recurring": [{"label": "Bin", "icon": "trash", "startDate": str(start), "step": 7}]})
    payload = main.build_display_cache(0)
    days = [d for d, l in _cell_labels(payload) if l == "Bin"]
    assert str(start) in days
    nxt = str(start + timedelta(days=7))
    all_days = {c["date"] for c in payload["cells"]}
    if nxt in all_days:            # next hit may fall outside the rendered grid
        assert nxt in days


def test_shifted_windows_never_overwrite_cache():
    """Only the window anchored on today is canonical. A ±N-week peek must not land
    in the cache, or Home would show whatever was last browsed."""
    _set_events({"events": []})
    main.build_display_cache(0)
    cached = json.loads(main.F_DISPLAY.read_text())
    off = main.build_display_cache(4)
    assert off["window_start"] != cached["window_start"]
    assert json.loads(main.F_DISPLAY.read_text())["window_start"] == cached["window_start"]


def test_birthday_holiday_text_colors_in_payload():
    """F5: configured birthdayText/holidayText ride in event_colors and the legend."""
    s = main.read_settings()
    s["eventColors"] = {"birthday": "#ffff00", "holiday": "#ff0000",
                        "birthdayText": "#112233", "holidayText": "#445566"}
    s.setdefault("display", {})["showHolidays"] = True
    main.write(main.F_SETTINGS, s)
    payload = main.build_display_cache(0)
    ec = payload["event_colors"]
    assert ec["birthdayText"] == "#112233" and ec["holidayText"] == "#445566"
    bl = next(l for l in payload["legend"] if l["label"] == "Birthday")
    hl = next(l for l in payload["legend"] if l["label"] == "Holiday")
    assert bl["text_color"] == "#112233" and hl["text_color"] == "#445566"


def test_birthday_holiday_text_colors_default_when_unset():
    """Old installs (no text keys) keep the original hardcoded pair."""
    s = main.read_settings()
    s["eventColors"] = {"birthday": "#ffff00", "holiday": "#ff0000"}   # no *Text keys
    main.write(main.F_SETTINGS, s)
    ec = main.build_display_cache(0)["event_colors"]
    assert ec["birthdayText"] == "#000000" and ec["holidayText"] == "#ffffff"
