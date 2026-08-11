"""_sanitize_event / _sanitize_recurring — the gate for all untrusted input
(relay commands today, mailsync inbound later)."""
import main


def test_event_valid_passes():
    out = main._sanitize_event({"date": "2026-08-01", "who": "family", "icon": "swim", "label": "Swim"})
    assert out == {"date": "2026-08-01", "who": "family", "icon": "swim", "label": "Swim"}


def test_event_unknown_member_falls_back_to_family():
    out = main._sanitize_event({"date": "2026-08-01", "who": "hacker", "icon": "swim", "label": "x"})
    assert out["who"] == "family"


def test_event_unknown_icon_falls_back_to_star():
    out = main._sanitize_event({"date": "2026-08-01", "who": "family", "icon": "<script>", "label": "x"})
    assert out["icon"] == "star"


def test_event_bad_date_rejected():
    assert main._sanitize_event({"date": "01/08/2026", "label": "x"}) is None
    assert main._sanitize_event({"date": "", "label": "x"}) is None
    assert main._sanitize_event({"label": "x"}) is None


def test_event_empty_label_rejected():
    assert main._sanitize_event({"date": "2026-08-01", "label": "   "}) is None


def test_event_label_capped_at_80():
    out = main._sanitize_event({"date": "2026-08-01", "label": "x" * 200})
    assert len(out["label"]) == 80


def test_recurring_valid_passes():
    out = main._sanitize_recurring({"label": "Garbage", "icon": "trash", "startDate": "2026-08-01",
                                    "step": 7, "iconOnly": True})
    assert out == {"label": "Garbage", "icon": "trash", "startDate": "2026-08-01",
                   "step": 7, "iconOnly": True}


def test_recurring_step_bounds():
    base = {"label": "x", "startDate": "2026-08-01"}
    assert main._sanitize_recurring({**base, "step": 0}) is None
    assert main._sanitize_recurring({**base, "step": 3651}) is None
    assert main._sanitize_recurring({**base, "step": "7"})["step"] == 7
    assert main._sanitize_recurring({**base, "step": "abc"}) is None


def test_recurring_bad_start_rejected():
    assert main._sanitize_recurring({"label": "x", "startDate": "soon", "step": 7}) is None
