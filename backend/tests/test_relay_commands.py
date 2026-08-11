"""Relay NAS-side pieces: the calendar/recipe mirror (aligned to mobile.html —
no birthdays/recurring), the recipes signature handshake, shopping republish,
inbox drain of retired command types, healthz."""
import asyncio
import json
from datetime import date, timedelta

import ai
import main
import relay_client
from fastapi import HTTPException
import shopping
import storage


class _CapClient:
    """Minimal async httpx.AsyncClient stand-in that records the posted body."""
    captured: dict = {}

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        _CapClient.captured = {"url": url, "json": json}
        class _R: status_code = 200
        return _R()


def _relay_on(monkeypatch):
    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "tok")
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _CapClient)
    _CapClient.captured = {}


def test_publish_sends_rolling_window_from_today(monkeypatch):
    # The phone always gets the days-ahead window anchored on today, regardless of
    # any week argument passed by the caller.
    _relay_on(monkeypatch)
    assert asyncio.run(shopping.publish_shopping("2099-01"))   # arg is ignored
    j = _CapClient.captured["json"]
    assert j["week"] == shopping._current_week_key()
    assert j["start"] == date.today().isoformat()
    assert "days" in j and "extras" in j and "have" in j


def test_rolling_payload_crosses_iso_week_boundary(monkeypatch):
    # A rolling window straddling Sun→Mon must pull each day's meal from its own
    # ISO week, and merge 'have'/'extras' across both weeks.
    sun = date(2026, 1, 4)
    while sun.weekday() != 6:                                  # advance to a Sunday
        sun += timedelta(days=1)
    mon = sun + timedelta(days=1)
    wk_sun, wk_mon = shopping._week_key_of(sun), shopping._week_key_of(mon)
    assert wk_sun != wk_mon
    plans = {wk_sun: [None]*6 + [{"id": "r-sun", "name": "Sunday roast"}],
             wk_mon: [{"id": "r-mon", "name": "Monday pasta"}] + [None]*6}
    recipes = {"r-sun": {"ingredients": [{"item": "Beef", "amount": "1", "unit": "kg"}]},
               "r-mon": {"ingredients": [{"item": "Pasta", "amount": "500", "unit": "g"}]}}
    monkeypatch.setattr(shopping, "read_meals", lambda: {"plan": plans})
    monkeypatch.setattr(shopping, "read_shopping",
                        lambda: {wk_sun: {"have": ["Salt"], "extras": [{"item": "Foil"}]}})
    monkeypatch.setattr(shopping, "read_recipe_file", lambda rid: recipes.get(rid))
    p = shopping._rolling_shopping_payload(start=sun, span=2)
    assert [d["name"] for d in p["days"]] == ["Sunday roast", "Monday pasta"]
    assert [d["date"] for d in p["days"]] == [sun.isoformat(), mon.isoformat()]
    assert p["weekKey"] == wk_sun and p["start"] == sun.isoformat()
    assert p["have"] == ["Salt"]                              # merged from the touched weeks
    assert [e["item"] for e in p["extras"]] == ["Foil"]


def test_republish_noop_when_relay_unconfigured(monkeypatch):
    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "")
    assert asyncio.run(shopping.republish_shopping()) is False


def test_sanitize_recurring_carries_valid_who():
    out = storage._sanitize_recurring({"label": "Gym", "startDate": "2026-08-03",
                                       "freq": "weekly", "who": "owner"})
    assert out["who"] == "owner"
    out = storage._sanitize_recurring({"label": "Gym", "startDate": "2026-08-03",
                                       "step": 7, "who": "hacker"})
    assert "who" not in out                     # unknown member → omitted, not coerced


class _FakeRelay:
    """Stateful stand-in for the relay's /calendar/publish: stores the recipes
    signature like the real server and echoes it back (the NAS handshake)."""
    stored_sig = ""
    captured: dict = {}

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        _FakeRelay.captured = {"url": url, "json": json}
        if isinstance(json, dict) and isinstance(json.get("recipes"), dict):
            _FakeRelay.stored_sig = json["recipes"].get("sig", "")
        sig = _FakeRelay.stored_sig
        class _R:
            status_code = 200
            def json(self): return {"ok": True, "recipesSig": sig}
        return _R()


def _mirror_on(monkeypatch):
    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "tok")
    monkeypatch.setattr(relay_client.httpx, "AsyncClient", _FakeRelay)
    monkeypatch.setattr(relay_client, "_relay_recipes_sig", None)
    _FakeRelay.stored_sig = ""
    _FakeRelay.captured = {}


def test_mirror_excludes_birthdays_and_recurring(monkeypatch):
    """Aligned to mobile.html: birthdays (DOB-grade PII) and recurring items
    never reach the internet-facing relay; upcoming events still do."""
    main.write(main.F_EVENTS, {
        "events": [{"id": "e1", "date": "2099-01-01", "who": "family", "icon": "star", "label": "Future"}],
        "birthdays": [{"id": "b1", "name": "Mom", "month": 5, "day": 7, "year": 1960}],
        "recurring": [{"id": "r1", "label": "Bins", "icon": "trash", "startDate": "2026-01-01", "step": 7}],
    })
    _mirror_on(monkeypatch)
    assert asyncio.run(relay_client.publish_calendar())
    p = _FakeRelay.captured["json"]
    assert p["events"][0]["id"] == "e1"
    assert "birthdays" not in p and "recurring" not in p
    assert "1960" not in __import__("json").dumps(p)


def test_mirror_recipes_sent_once_then_skipped(monkeypatch):
    """The ~200 KB library blob rides along only when the relay's stored
    signature differs; steady-state publishes stay small."""
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    storage.write_recipe_file("test-soup", {"id": "test-soup", "name": "Test soup",
                                            "course": "soup", "ingredients": [], "steps": [],
                                            "photos": ["x.jpg"], "log": {"entered_by": "owner"},
                                            "source": {"type": "url", "value": "https://x"}})
    storage.rebuild_recipe_index()
    _mirror_on(monkeypatch)

    assert asyncio.run(relay_client.publish_calendar())
    p1 = _FakeRelay.captured["json"]
    assert p1["recipesSig"] and p1["recipes"]["sig"] == p1["recipesSig"]
    rec = p1["recipes"]["records"]["test-soup"]
    assert rec["name"] == "Test soup"
    for private in ("photos", "log", "source"):     # content only — provenance stays home
        assert private not in rec

    assert asyncio.run(relay_client.publish_calendar())   # relay now has the sig
    p2 = _FakeRelay.captured["json"]
    assert "recipes" not in p2 and p2["recipesSig"] == p1["recipesSig"]


def test_drain_applies_events_drops_retired_types(monkeypatch):
    """Old queued birthday/recurring commands (pre-alignment phones) are acked
    and dropped — never applied — while event commands still work."""
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    inbox = [
        {"id": "c1", "type": "event",
         "payload": {"date": "2099-05-01", "who": "family", "icon": "star", "label": "Party"}},
        {"id": "c2", "type": "birthday", "payload": {"name": "Mom", "month": 5, "day": 7}},
        {"id": "c3", "type": "recurring", "payload": {"label": "Bins", "startDate": "2099-01-01", "step": 7}},
    ]
    acked = {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            class _R:
                status_code = 200
                def json(self): return {"items": inbox}
            return _R()
        async def post(self, url, headers=None, json=None):
            acked.update(json or {})
            class _R: status_code = 200
            return _R()

    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "tok")
    monkeypatch.setattr(relay_client.httpx, "AsyncClient", FakeClient)
    asyncio.run(relay_client.drain_relay_inbox())

    ev = main.read_events()
    assert [e["label"] for e in ev["events"]] == ["Party"]
    assert ev["birthdays"] == [] and ev["recurring"] == []
    assert set(acked.get("ids", [])) == {"c1", "c2", "c3"}   # all acked, retired ones dropped


def _drain_with(monkeypatch, inbox):
    acked = {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            class _R:
                status_code = 200
                def json(self): return {"items": inbox}
            return _R()
        async def post(self, url, headers=None, json=None):
            acked.update(json or {})
            class _R: status_code = 200
            return _R()

    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "tok")
    monkeypatch.setattr(relay_client.httpx, "AsyncClient", FakeClient)
    asyncio.run(relay_client.drain_relay_inbox())
    return acked


def test_drain_recipe_photo_saves_flagged_for_review(monkeypatch, tmp_path):
    """A phone photo command AI-extracts and saves straight to the library,
    flagged needs_review; the photo bytes are attached to the recipe."""
    import base64
    import recipes

    async def fake_extract(images, translate=False):
        assert not translate
        return {"name": "Fırında Karnabahar", "description": "",
                "ingredients": [{"item": "karnabahar", "amount": "1", "unit": ""}],
                "steps": [{"text": "Fırınla."}]}

    monkeypatch.setattr(recipes, "_ai_extract_recipe_image", fake_extract)
    img = base64.b64encode(b"\xff\xd8fakejpegbytes").decode()
    acked = _drain_with(monkeypatch, [
        {"id": "p1", "type": "recipe_photo", "payload": {"image": img, "media": "image/jpeg"}}])
    assert acked.get("ids") == ["p1"]

    idx = [e for e in storage.read_recipe_index() if e["name"] == "Fırında Karnabahar"]
    assert len(idx) == 1 and idx[0]["needs_review"] is True
    rec = storage.read_recipe_file(idx[0]["id"])
    assert rec["needs_review"] is True
    assert rec["ingredients"][0]["item"] == "karnabahar"   # original language kept
    assert rec["photos"], "the scanned photo must be attached"
    assert rec["source"]["type"] == "photo"


def test_drain_recipe_photo_duplicate_goes_to_pending(monkeypatch):
    """A scan matching an existing recipe queues a pending merge (F8c) instead
    of creating a duplicate."""
    import base64
    import recipes

    storage.write_recipe_file("existing-soup", {"id": "existing-soup", "name": "Mercimek Çorbası",
                                                "ingredients": [], "steps": []})
    storage.rebuild_recipe_index()

    async def fake_extract(images, translate=False):
        return {"name": "Mercimek Corbasi", "ingredients": [], "steps": []}

    monkeypatch.setattr(recipes, "_ai_extract_recipe_image", fake_extract)
    before = {e["id"] for e in storage.read_recipe_index()}
    img = base64.b64encode(b"\xff\xd8fake").decode()
    _drain_with(monkeypatch, [
        {"id": "p2", "type": "recipe_photo", "payload": {"image": img, "media": "image/jpeg"}}])

    assert {e["id"] for e in storage.read_recipe_index()} == before   # no new recipe
    assert any(d.get("matchId") == "existing-soup" for d in storage.list_pending_recipes())


def test_drain_recipe_photo_bad_image_dropped(monkeypatch):
    """Corrupt base64 must not poison the queue: acked + dropped, nothing saved."""
    import recipes

    async def boom(images, translate=False):   # must never be reached
        raise AssertionError("extractor called for a bad image")

    monkeypatch.setattr(recipes, "_ai_extract_recipe_image", boom)
    before = {e["id"] for e in storage.read_recipe_index()}
    acked = _drain_with(monkeypatch, [
        {"id": "p3", "type": "recipe_photo", "payload": {"image": "not-base64!!!"}}])
    assert acked.get("ids") == ["p3"]
    assert {e["id"] for e in storage.read_recipe_index()} == before


def test_drain_ai_command_fuse(monkeypatch):
    """At most AI_CMD_FUSE paid-AI commands run per drain cycle; the overflow
    stays queued (NOT acked) and non-AI commands are unaffected. A leaked write
    token can't burn API credit faster than the fuse rate."""
    import recipes
    main.write(main.F_EVENTS, {"events": [], "birthdays": [], "recurring": []})
    calls = []

    async def fake_import(p):
        calls.append(p["url"])

    monkeypatch.setattr(relay_client, "_import_recipe", fake_import)
    monkeypatch.setattr(relay_client, "AI_CMD_FUSE", 2)
    inbox = [{"id": f"i{n}", "type": "recipe_import",
              "payload": {"url": f"https://x.test/{n}"}} for n in range(4)]
    inbox.append({"id": "e1", "type": "event",
                  "payload": {"date": "2099-06-01", "who": "family", "icon": "star", "label": "After fuse"}})

    acked = _drain_with(monkeypatch, inbox)
    assert len(calls) == 2                                   # fuse held
    assert set(acked["ids"]) == {"i0", "i1", "e1"}           # overflow NOT acked
    assert [e["label"] for e in main.read_events()["events"]] == ["After fuse"]

    acked = _drain_with(monkeypatch, inbox[2:4])             # next cycle drains the rest
    assert len(calls) == 4
    assert set(acked["ids"]) == {"i2", "i3"}


def test_drain_shopping_extra_add_and_remove(monkeypatch):
    """F9: a phone shopping_extra command adds/removes a manual item in
    shopping.json (not an AI command — no fuse) and is acked."""
    storage.write_shopping({})
    acked = _drain_with(monkeypatch, [
        {"id": "x1", "type": "shopping_extra", "payload": {"week": "2026-41", "item": "Batteries"}}])
    assert acked.get("ids") == ["x1"]
    assert [e["item"] for e in storage.read_shopping()["2026-41"]["extras"]] == ["Batteries"]

    acked = _drain_with(monkeypatch, [
        {"id": "x2", "type": "shopping_extra",
         "payload": {"week": "2026-41", "item": "batteries", "action": "remove"}}])
    assert acked.get("ids") == ["x2"]
    assert storage.read_shopping()["2026-41"]["extras"] == []


def test_drain_shopping_extra_missing_week_dropped(monkeypatch):
    """A shopping_extra without a week is malformed: acked + dropped, nothing written."""
    storage.write_shopping({})
    acked = _drain_with(monkeypatch, [
        {"id": "x3", "type": "shopping_extra", "payload": {"item": "Batteries"}}])
    assert acked.get("ids") == ["x3"]
    assert storage.read_shopping() == {}


def test_manual_save_clears_needs_review(client):
    storage.write_recipe_file("scan-1", {"id": "scan-1", "name": "Scan", "needs_review": True,
                                         "ingredients": [], "steps": []})
    storage.rebuild_recipe_index()
    body = storage.read_recipe_file("scan-1")
    assert client.put("/recipes/scan-1", json=body).status_code == 200
    assert "needs_review" not in storage.read_recipe_file("scan-1")
    assert [e for e in storage.read_recipe_index() if e["id"] == "scan-1"][0]["needs_review"] is False


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_embed_csp_header(client, monkeypatch):
    """With EMBED_ORIGIN set, every response allows the relay app's AI content
    tab to iframe this backend; unset keeps the LAN-only posture (no header)."""
    monkeypatch.setattr(main, "EMBED_ORIGIN", "https://relay.example")
    r = client.get("/settings")
    assert r.headers["content-security-policy"] == "frame-ancestors 'self' https://relay.example"
    monkeypatch.setattr(main, "EMBED_ORIGIN", "")
    assert "content-security-policy" not in client.get("/settings").headers


# ── Retry budget (the token-drain fix) ────────────────────────────────────────
# Before this, a command that failed was left unacked and retried on every poll
# forever. A recipe whose extraction reply wouldn't parse therefore re-ran a paid
# model call every ~2 minutes until the provider's credit ran out.
def _fail_import_with(monkeypatch, exc):
    async def boom(p):
        raise exc
    monkeypatch.setattr(relay_client, "_import_recipe", boom)


def _attempts():
    import json as _json
    from storage import F_RELAY_ATTEMPTS
    return _json.loads(F_RELAY_ATTEMPTS.read_text()) if F_RELAY_ATTEMPTS.exists() else {}


def _reset_retry_state():
    from storage import F_RELAY_APPLIED, F_RELAY_ATTEMPTS
    for f in (F_RELAY_APPLIED, F_RELAY_ATTEMPTS):
        if f.exists():
            f.unlink()


def test_unparseable_reply_is_dropped_on_the_first_failure(monkeypatch):
    """The model answered and was billed, but the reply wasn't JSON. The same
    input would fail identically, so retrying only spends money — drop it."""
    _reset_retry_state()
    _fail_import_with(monkeypatch, json.JSONDecodeError("Expecting value", "{", 0))
    inbox = [{"id": "bad1", "type": "recipe_import", "payload": {"url": "https://x.test/1"}}]

    acked = _drain_with(monkeypatch, inbox)
    assert acked["ids"] == ["bad1"]                  # acked, so the relay stops serving it
    assert "bad1" not in _attempts()                 # no budget left dangling


def test_transient_failure_retries_then_gives_up_within_budget(monkeypatch):
    """A provider/network error might succeed next cycle, so it retries — but the
    budget is finite and persisted, so it cannot loop forever."""
    _reset_retry_state()
    monkeypatch.setattr(relay_client, "MAX_CMD_ATTEMPTS", 3)
    _fail_import_with(monkeypatch, HTTPException(502, "Could not reach the AI service"))
    inbox = [{"id": "flaky", "type": "recipe_import", "payload": {"url": "https://x.test/1"}}]

    assert _drain_with(monkeypatch, inbox).get("ids", []) == []    # cycle 1: not acked
    assert _attempts()["flaky"] == 1
    assert _drain_with(monkeypatch, inbox).get("ids", []) == []    # cycle 2: still retrying
    assert _attempts()["flaky"] == 2
    assert _drain_with(monkeypatch, inbox)["ids"] == ["flaky"]     # cycle 3: budget spent, dropped
    assert "flaky" not in _attempts()


def test_a_missing_api_key_never_spends_the_retry_budget(monkeypatch):
    """Nothing was sent and nothing was billed, and the fix is an env var — so the
    family's scanned recipe waits instead of being thrown away."""
    _reset_retry_state()
    monkeypatch.setattr(relay_client, "MAX_CMD_ATTEMPTS", 3)
    _fail_import_with(monkeypatch, ai.ProviderNotConfigured(500, "OPENROUTER_API_KEY not set"))
    inbox = [{"id": "nokey", "type": "recipe_import", "payload": {"url": "https://x.test/1"}}]

    for _ in range(5):
        assert _drain_with(monkeypatch, inbox).get("ids", []) == []
    assert _attempts() == {}                          # still queued, budget untouched


def test_a_successful_retry_clears_the_budget(monkeypatch):
    """A command that fails once and then works must not carry its failure count
    forward — the next unrelated hiccup deserves a full budget."""
    _reset_retry_state()
    monkeypatch.setattr(relay_client, "MAX_CMD_ATTEMPTS", 3)
    _fail_import_with(monkeypatch, HTTPException(502, "flaky"))
    inbox = [{"id": "ok1", "type": "recipe_import", "payload": {"url": "https://x.test/1"}}]
    _drain_with(monkeypatch, inbox)
    assert _attempts()["ok1"] == 1

    async def works(p):
        return None
    monkeypatch.setattr(relay_client, "_import_recipe", works)
    assert _drain_with(monkeypatch, inbox)["ids"] == ["ok1"]
    assert "ok1" not in _attempts()
