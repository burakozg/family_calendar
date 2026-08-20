"""F4: multi-day events — endDate (inclusive) expansion, cap, validation."""
from datetime import date, timedelta

import main


def _payload_days_with(payload, label):
    return [c["date"] for c in payload["cells"] for e in c["events"] if e["label"] == label]


def test_multiday_event_appears_on_each_day():
    d0 = date.today()
    d1 = d0 + timedelta(days=3)
    main.write(main.F_EVENTS, {"events": [
        {"id": "md1", "date": str(d0), "endDate": str(d1), "who": "family",
         "icon": "travel", "label": "Trip"},
    ], "birthdays": [], "recurring": []})
    days = _payload_days_with(main.build_display_cache(0), "Trip")
    grid = {c["date"] for c in main.build_display_cache(0)["cells"]}
    expected = [str(d0 + timedelta(days=i)) for i in range(4)]
    assert [d for d in expected if d in grid] == days


def test_multiday_capped_at_60_days():
    d0 = date.today().replace(day=1)
    main.write(main.F_EVENTS, {"events": [
        {"id": "md2", "date": str(d0), "endDate": str(d0 + timedelta(days=365)),
         "who": "family", "icon": "travel", "label": "Forever"},
    ], "birthdays": [], "recurring": []})
    payload = main.build_display_cache(12)  # 12 weeks out: past the 60-day cap → absent
    assert _payload_days_with(payload, "Forever") == []


def test_sanitizer_end_date_rules():
    base = {"date": "2026-08-10", "label": "x"}
    assert main._sanitize_event({**base, "endDate": "2026-08-12"})["endDate"] == "2026-08-12"
    assert "endDate" not in main._sanitize_event({**base, "endDate": "2026-08-01"})  # before start
    assert "endDate" not in main._sanitize_event({**base, "endDate": "2026-08-10"})  # same day
    assert "endDate" not in main._sanitize_event({**base, "endDate": "soonish"})


def test_end_before_start_in_store_is_tolerated():
    d0 = date.today()
    main.write(main.F_EVENTS, {"events": [
        {"id": "md3", "date": str(d0), "endDate": str(d0 - timedelta(days=5)),
         "who": "family", "icon": "star", "label": "Backwards"},
    ], "birthdays": [], "recurring": []})
    days = _payload_days_with(main.build_display_cache(0), "Backwards")
    assert days == [str(d0)]   # clamped to a single day, not dropped
