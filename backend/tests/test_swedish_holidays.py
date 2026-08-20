"""F5: locally-computed Swedish red days + their integration into the display
cache (holiday events, red-day cell flag, legend entry, toggle)."""
from datetime import date

import main
from swedish_holidays import _easter_sunday, swedish_holidays


def test_easter_known_years():
    # Western (Gregorian) Easter Sundays — reference values.
    assert _easter_sunday(2025) == date(2025, 4, 20)
    assert _easter_sunday(2026) == date(2026, 4, 5)
    assert _easter_sunday(2027) == date(2027, 3, 28)


def test_fixed_and_easter_derived_2026():
    h = swedish_holidays(2026)
    assert h["2026-01-01"] == "Nyårsdagen"
    assert h["2026-01-06"] == "Trettondedag jul"
    assert h["2026-04-03"] == "Långfredagen"          # Easter - 2
    assert h["2026-04-05"] == "Påskdagen"
    assert h["2026-04-06"] == "Annandag påsk"
    assert h["2026-05-01"] == "Första maj"
    assert h["2026-05-14"] == "Kristi himmelsfärdsdag"  # Easter + 39
    assert h["2026-05-24"] == "Pingstdagen"             # Easter + 49
    assert h["2026-06-06"] == "Sveriges nationaldag"
    assert h["2026-12-24"] == "Julafton"
    assert h["2026-12-25"] == "Juldagen"
    assert h["2026-12-26"] == "Annandag jul"
    assert h["2026-12-31"] == "Nyårsafton"


def test_floating_saturdays_are_saturdays_in_range():
    for yr in range(2024, 2031):
        h = swedish_holidays(yr)
        mid = next(date.fromisoformat(d) for d, n in h.items() if n == "Midsommardagen")
        alls = next(date.fromisoformat(d) for d, n in h.items() if n == "Alla helgons dag")
        assert mid.weekday() == 5 and mid.month == 6 and 20 <= mid.day <= 26
        assert alls.weekday() == 5 and (alls.month, alls.day) >= (10, 31)
        # Midsummer's Eve is the Friday right before Midsummer's Day.
        eve = next(date.fromisoformat(d) for d, n in h.items() if n == "Midsommarafton")
        assert (mid - eve).days == 1 and eve.weekday() == 4


# ── Display-cache integration ────────────────────────────────────────────────
def _reset_settings():
    s = main.read_settings()
    s.setdefault("display", {})["showHolidays"] = True
    main.write(main.F_SETTINGS, s)


def test_holidays_flagged_and_labelled_when_enabled():
    _reset_settings()
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    seen = {}
    for off in range(-4, 56, 4):                    # sweep a full year of 4-week windows
        payload = main.build_display_cache(off)
        for c in payload["cells"]:
            if c.get("holiday"):
                assert any(e["type"] == "holiday" for e in c["events"])
                seen[c["date"]] = True
    assert len(seen) >= 10                            # red days really showed up
    assert any(l["label"] == "Holiday" for l in main.build_display_cache(0)["legend"])


def test_toggle_off_removes_holidays():
    s = main.read_settings()
    s.setdefault("display", {})["showHolidays"] = False
    main.write(main.F_SETTINGS, s)
    try:
        for off in range(-4, 56, 4):
            payload = main.build_display_cache(off)
            assert all(not c.get("holiday") for c in payload["cells"])
            assert all(e["type"] != "holiday"
                       for c in payload["cells"] for e in c["events"])
        assert all(l["label"] != "Holiday"
                   for l in main.build_display_cache(0)["legend"])
    finally:
        _reset_settings()                            # don't leak state to other tests
