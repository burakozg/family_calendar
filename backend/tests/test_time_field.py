"""F3: optional HH:MM time on events/recurring — validation and within-day
ordering (birthdays first, then untimed, then timed ascending). The time is
not shown in the label; it only drives the sort."""
from datetime import date

import main


def test_sanitizer_accepts_valid_time():
    out = main._sanitize_event({"date": "2026-08-01", "label": "x", "time": "14:30"})
    assert out["time"] == "14:30"
    rec = main._sanitize_recurring({"label": "x", "startDate": "2026-08-01",
                                    "step": 7, "time": "07:05"})
    assert rec["time"] == "07:05"


def test_sanitizer_drops_invalid_time():
    for bad in ("24:00", "9:00", "12:60", "noonish", "12:00:00"):
        out = main._sanitize_event({"date": "2026-08-01", "label": "x", "time": bad})
        assert "time" not in out, bad


def test_cache_omits_time_from_label_and_sorts_day():
    """Time is never shown in the label — it only drives within-day order,
    with birthdays pinned to the top."""
    d = str(date.today())
    today = date.today()
    main.write(main.F_EVENTS, {"events": [
        {"id": "t1", "date": d, "who": "family", "icon": "star", "label": "Late", "time": "18:00"},
        {"id": "t2", "date": d, "who": "family", "icon": "star", "label": "Early", "time": "08:15"},
        {"id": "t3", "date": d, "who": "family", "icon": "star", "label": "AllDay"},
    ], "birthdays": [
        {"name": "Kiddo", "month": today.month, "day": today.day},
    ], "recurring": []})
    payload = main.build_display_cache(0)
    cell = next(c for c in payload["cells"] if c["date"] == d)
    labels = [e["label"] for e in cell["events"]]
    assert labels == ["Kiddo", "AllDay", "Early", "Late"]
    assert [e.get("time", "") for e in cell["events"]] == ["", "", "08:15", "18:00"]
