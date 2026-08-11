"""F2: recurrence v2 — occurrence engine (legacy step + daily/weekly with
byday/until/exdates), sanitizer rules, and cache integration."""
from datetime import date, timedelta

import main


def occ(r, start, end):
    return [str(d) for d in main._recurring_occurrences(r, date.fromisoformat(start),
                                                        date.fromisoformat(end))]


def test_legacy_step():
    r = {"startDate": "2026-07-01", "step": 7}
    assert occ(r, "2026-07-01", "2026-07-22") == \
        ["2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22"]


def test_daily_jump_ahead_from_far_past():
    r = {"startDate": "2000-01-03", "freq": "daily", "interval": 10}
    got = occ(r, "2026-07-01", "2026-07-31")
    assert got and all(d >= "2026-07-01" for d in got)
    # Alignment: every date is start + k*10 days.
    d0 = date(2000, 1, 3)
    assert all((date.fromisoformat(d) - d0).days % 10 == 0 for d in got)


def test_weekly_multiple_bydays():
    # 2026-07-06 is a Monday.
    r = {"startDate": "2026-07-06", "freq": "weekly", "interval": 1, "byday": ["MO", "TH"]}
    assert occ(r, "2026-07-06", "2026-07-19") == \
        ["2026-07-06", "2026-07-09", "2026-07-13", "2026-07-16"]


def test_weekly_interval_two():
    r = {"startDate": "2026-07-06", "freq": "weekly", "interval": 2, "byday": ["MO"]}
    assert occ(r, "2026-07-06", "2026-08-03") == \
        ["2026-07-06", "2026-07-20", "2026-08-03"]


def test_weekly_defaults_to_start_weekday():
    r = {"startDate": "2026-07-08", "freq": "weekly", "interval": 1}   # a Wednesday
    assert occ(r, "2026-07-08", "2026-07-21") == ["2026-07-08", "2026-07-15"]


def test_until_and_exdates_apply_to_both_forms():
    r = {"startDate": "2026-07-01", "step": 7, "until": "2026-07-15",
         "exdates": ["2026-07-08"]}
    assert occ(r, "2026-07-01", "2026-12-31") == ["2026-07-01", "2026-07-15"]
    r2 = {"startDate": "2026-07-06", "freq": "weekly", "byday": ["MO"],
          "until": "2026-07-20", "exdates": ["2026-07-13"]}
    assert occ(r2, "2026-07-01", "2026-12-31") == ["2026-07-06", "2026-07-20"]


def test_window_before_start_is_empty():
    r = {"startDate": "2026-09-01", "step": 7}
    assert occ(r, "2026-07-01", "2026-07-31") == []


def test_sanitizer_v2_weekly():
    out = main._sanitize_recurring({"label": "Training", "startDate": "2026-07-06",
                                    "freq": "weekly", "interval": 2,
                                    "byday": ["TH", "MO", "XX", "MO"],
                                    "until": "2026-12-01", "exdates": ["2026-07-13", "junk"]})
    assert out["freq"] == "weekly" and out["interval"] == 2
    assert out["byday"] == ["MO", "TH"]            # deduped, validated, week-ordered
    assert out["until"] == "2026-12-01"
    assert out["exdates"] == ["2026-07-13"]


def test_sanitizer_v2_rejects_bad_interval():
    base = {"label": "x", "startDate": "2026-07-06", "freq": "weekly"}
    assert main._sanitize_recurring({**base, "interval": 0}) is None
    assert main._sanitize_recurring({**base, "interval": 999}) is None
    assert main._sanitize_recurring({**base, "interval": "abc"}) is None


def test_sanitizer_legacy_still_works():
    out = main._sanitize_recurring({"label": "Bin", "startDate": "2026-07-06", "step": 14})
    assert out["step"] == 14 and "freq" not in out


def test_cache_renders_weekly_byday():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": [
        {"id": "r1", "label": "Gym", "icon": "gym", "startDate": str(monday - timedelta(days=28)),
         "freq": "weekly", "interval": 1, "byday": ["MO", "FR"], "iconOnly": True},
    ]})
    payload = main.build_display_cache(0)
    hit_days = {c["date"] for c in payload["cells"]
                for e in c["events"] if e["label"] == "Gym"}
    assert str(monday) in hit_days
    assert str(monday + timedelta(days=4)) in hit_days       # Friday
    assert str(monday + timedelta(days=1)) not in hit_days   # Tuesday
