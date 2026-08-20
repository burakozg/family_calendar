"""The Inky calendar is a rolling 4-week window, not a calendar month.

The property that matters, and the reason the change was made: today's marker lives
in the top row and stays there. A month grid walked it downward as the month wore on,
so by the last week the screen was mostly spent days with almost no forward view.
"""
from datetime import date, timedelta

import display_cache
import main
import pytest


@pytest.fixture(autouse=True)
def _no_events():
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})


def _row_of_today(payload):
    """(row, col) of the cell flagged `today`, or None."""
    for i, c in enumerate(payload["cells"]):
        if c["today"]:
            return divmod(i, 7)
    return None


# ── Shape ────────────────────────────────────────────────────────────────────
def test_window_is_four_monday_start_weeks():
    p = main.build_display_cache(0)
    assert len(p["cells"]) == 28
    first = date.fromisoformat(p["cells"][0]["date"])
    assert first.weekday() == 0                                  # Monday
    assert p["window_start"] == p["cells"][0]["date"]
    assert p["window_end"] == p["cells"][-1]["date"]
    # Contiguous, no gaps or repeats
    dates = [date.fromisoformat(c["date"]) for c in p["cells"]]
    assert dates == [first + timedelta(days=i) for i in range(28)]


def test_today_is_in_the_top_row_and_only_there():
    p = main.build_display_cache(0)
    pos = _row_of_today(p)
    assert pos is not None, "today must appear in the unshifted window"
    assert pos[0] == 0, f"today landed in row {pos[0]}, not the top row"
    assert sum(1 for c in p["cells"] if c["today"]) == 1


def test_offset_slides_the_window_by_whole_weeks():
    base = date.fromisoformat(main.build_display_cache(0)["window_start"])
    for off in (-4, -3, -1, 1, 4, 12):
        p = main.build_display_cache(off)
        assert date.fromisoformat(p["window_start"]) == base + timedelta(weeks=off)
        assert len(p["cells"]) == 28
    # Shifting BACK keeps today on screen (the window is 4 weeks long) but pushes it
    # down out of the top row; only the unshifted window anchors it at row 0.
    for off in (-1, -2, -3):
        assert _row_of_today(main.build_display_cache(off)) == (-off, date.today().weekday())
    # Shifting a full window either way takes today off screen entirely.
    for off in (-4, 4, 12):
        assert _row_of_today(main.build_display_cache(off)) is None


# ── The actual complaint ─────────────────────────────────────────────────────
@pytest.mark.parametrize("weekday", range(7))
def test_marker_crosses_the_top_row_and_never_descends(monkeypatch, weekday):
    """Walk a whole week a day at a time. In a month grid the marker moves down a row
    every Monday; here it must stay in row 0 and only advance its column."""
    monday = date(2026, 8, 24)                                   # a known Monday
    frozen = monday + timedelta(days=weekday)

    class _Date(date):
        @classmethod
        def today(cls):
            return frozen

    monkeypatch.setattr(display_cache, "date", _Date)
    p = display_cache.build_display_cache(0)
    assert _row_of_today(p) == (0, weekday)
    assert p["cells"][0]["date"] == str(monday)                  # window never moves mid-week


def test_window_rolls_up_when_the_week_turns(monkeypatch):
    """Sunday evening → Monday morning: the marker returns to column 0 of the SAME
    row, and the window itself has advanced by exactly one week."""
    def _window_on(day):
        class _Date(date):
            @classmethod
            def today(cls):
                return day
        monkeypatch.setattr(display_cache, "date", _Date)
        return display_cache.build_display_cache(0)

    sunday = _window_on(date(2026, 8, 30))
    monday = _window_on(date(2026, 8, 31))
    assert _row_of_today(sunday) == (0, 6)
    assert _row_of_today(monday) == (0, 0)
    assert (date.fromisoformat(monday["window_start"])
            - date.fromisoformat(sunday["window_start"])) == timedelta(weeks=1)


# ── Title and month boundary ─────────────────────────────────────────────────
def test_title_names_the_range():
    assert display_cache._window_title(date(2026, 8, 3), date(2026, 8, 30)) == "August 2026"
    assert display_cache._window_title(date(2026, 8, 24), date(2026, 9, 20)) == "Aug - Sep 2026"
    assert display_cache._window_title(date(2026, 12, 21), date(2027, 1, 17)) == "Dec 2026 - Jan 2027"


def test_title_is_ascii_for_the_device_font():
    """bitmap8 has no glyph above 126, so an en dash would render as garbage."""
    for off in range(-4, 56, 4):
        assert main.build_display_cache(off)["title"].isascii()


def test_month_short_marks_exactly_the_first_of_a_month():
    """With no off-month shading, the 1st naming itself IS the month boundary."""
    for off in range(0, 52, 4):
        p = main.build_display_cache(off)
        for c in p["cells"]:
            if date.fromisoformat(c["date"]).day == 1:
                assert c["month_short"] == date.fromisoformat(c["date"]).strftime("%b")
            else:
                assert "month_short" not in c


def test_off_month_flag_is_gone():
    """`current_month` drove the hatch shading that a rolling window can't use; both
    renderers dropped it, so leaving it in the payload would only mislead."""
    assert all("current_month" not in c for c in main.build_display_cache(0)["cells"])


# ── API ──────────────────────────────────────────────────────────────────────
def test_week_offset_route(client):
    a = client.get("/display-data?week_offset=4").json()
    assert a["window_start"] == main.build_display_cache(4)["window_start"]
    assert len(a["cells"]) == 28


def test_legacy_month_offset_still_serves_a_window(client):
    """Device firmware drifts from this repo. A backend deployed ahead of a reflash
    must not hand an old Inky an offset it silently ignores."""
    legacy = client.get("/display-data?month_offset=1").json()
    modern = client.get("/display-data?week_offset=4").json()
    assert legacy["window_start"] == modern["window_start"]
    back = client.get("/display-data?month_offset=-1").json()
    assert back["window_start"] == client.get("/display-data?week_offset=-4").json()["window_start"]


def test_unshifted_request_is_the_cached_window(client):
    p = client.get("/display-data").json()
    assert p["window_start"] == main.build_display_cache(0)["window_start"]
    assert _row_of_today(p) == (0, date.today().weekday())
