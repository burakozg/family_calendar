"""Route-level tests through the TestClient: CRUD flows, the Host allowlist,
log filters, and the shopping state roundtrip.

Note: the body-size cap (413) is not testable through TestClient — httpx owns
the Content-Length header — it is covered by the live curl check in the S3
commit; the middleware logic itself is trivial header comparison."""
import main


def _event_count(client):
    return len(client.get("/events").json()["events"])


def test_settings_roundtrip(client):
    s = client.get("/settings").json()
    assert "familyName" in s and "members" in s


def test_event_add_then_delete(client):
    n = _event_count(client)
    r = client.post("/events/add", json={"date": "2026-08-02", "who": "family",
                                         "icon": "star", "label": "ApiTest"})
    assert r.status_code == 200 and _event_count(client) == n + 1
    events = client.get("/events").json()["events"]
    idx = next(i for i, e in enumerate(events) if e["label"] == "ApiTest")
    client.delete(f"/events/{idx}")
    assert _event_count(client) == n


def test_mutations_are_logged_with_who(client):
    client.post("/events/add", headers={"X-Who": "owner"},
                json={"date": "2026-08-03", "who": "owner", "icon": "star", "label": "WhoTest"})
    entries = client.get("/logs", params={"category": "data", "q": "WhoTest"}).json()["entries"]
    assert entries and entries[0]["who"] == "owner"


def test_birthday_year_validation(client):
    client.post("/birthdays/add", json={"name": "YearOk", "month": 1, "day": 2, "year": 1990})
    client.post("/birthdays/add", json={"name": "YearBad", "month": 1, "day": 3, "year": 1500})
    bds = {b["name"]: b for b in client.get("/events").json()["birthdays"]}
    assert bds["YearOk"].get("year") == 1990
    assert "year" not in bds["YearBad"]


def test_recurring_add_delete(client):
    client.post("/recurring/add", json={"label": "RecApi", "icon": "trash",
                                        "startDate": "2026-08-01", "step": 14, "iconOnly": True})
    rec = client.get("/events").json()["recurring"]
    idx = next(i for i, r in enumerate(rec) if r["label"] == "RecApi")
    client.delete(f"/recurring/{idx}")
    assert all(r["label"] != "RecApi" for r in client.get("/events").json()["recurring"])


def test_host_allowlist(client):
    assert client.get("/settings").status_code == 200                      # "testserver" allowlisted
    r = client.get("/settings", headers={"Host": "evil.example.com"})
    assert r.status_code == 421
    assert client.get("/settings", headers={"Host": "192.168.1.7:8000"}).status_code == 200
    assert client.get("/settings", headers={"Host": "[::1]:8000"}).status_code == 200


def test_host_allowed_unit():
    assert main._host_allowed("10.1.2.3:8000")
    assert main._host_allowed("localhost")
    assert not main._host_allowed("evil.example.com")
    assert not main._host_allowed("")


def test_meals_roundtrip(client):
    plan = [{"id": None, "name": "Tacos", "notes": ""}] * 7
    client.patch("/meals/plan", json={"weekKey": "2026-40", "meals": plan})
    assert client.get("/meals").json()["plan"]["2026-40"][0]["name"] == "Tacos"


def test_shopping_state_roundtrip(client):
    client.post("/shopping/2026-40", json={"have": ["salt"], "bought": ["milk"]})
    got = client.get("/shopping/2026-40").json()
    assert got["have"] == ["salt"] and got["bought"] == ["milk"]


def test_shopping_extra_add_delete_and_state_preserves_extras(client):
    # F9: add a manual extra, confirm it appears aisle-tagged.
    assert client.post("/shopping/2026-41/extra", json={"item": "Batteries", "who": "owner"}).status_code == 200
    got = client.get("/shopping/2026-41").json()
    assert [e["item"] for e in got["extras"]] == ["Batteries"]
    assert got["extras"][0]["category"] == "Household" or got["extras"][0]["category"] == "Other"

    # Empty item is rejected; saving check-off state must not wipe the extra.
    assert client.post("/shopping/2026-41/extra", json={"item": "  "}).status_code == 400
    client.post("/shopping/2026-41", json={"have": [], "bought": ["batteries"]})
    assert [e["item"] for e in client.get("/shopping/2026-41").json()["extras"]] == ["Batteries"]

    # Delete removes it (case-insensitive by name).
    assert client.post("/shopping/2026-41/extra/delete", json={"item": "batteries"}).status_code == 200
    assert client.get("/shopping/2026-41").json()["extras"] == []


def test_logs_since_filter(client):
    assert client.get("/logs", params={"since": "2099-01-01"}).json()["entries"] == []
    assert client.get("/logs", params={"since": "2000-01-01"}).json()["entries"]


def test_ai_routes_fail_closed_without_key(client):
    r = client.post("/meals/plan/generate", json={"weekKey": "2026-40", "recent": [], "events": {}})
    assert r.status_code == 500   # ANTHROPIC_API_KEY is unset in tests


def test_ssrf_guard_ip_literals():
    assert not main._host_is_public("127.0.0.1")
    assert not main._host_is_public("10.0.0.5")
    assert not main._host_is_public("192.168.1.1")
    assert not main._host_is_public("169.254.169.254")   # cloud metadata
    assert not main._host_is_public("::1")
    assert main._host_is_public("1.1.1.1")
