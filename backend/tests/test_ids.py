"""A7: stable ids — lazy migration, assignment on add, by-id deletes."""
import main


def test_read_events_migrates_missing_ids():
    main.write(main.F_EVENTS, {"events": [{"date": "2026-08-01", "who": "family",
                                           "icon": "star", "label": "NoId"}],
                               "birthdays": [{"name": "B", "month": 1, "day": 1}],
                               "recurring": []})
    ev = main.read_events()
    assert ev["events"][0]["id"]
    assert ev["birthdays"][0]["id"]
    # Persisted, and stable across reads.
    again = main.read_events()
    assert again["events"][0]["id"] == ev["events"][0]["id"]


def test_post_events_assigns_and_preserves_ids(client):
    doc = client.get("/events").json()
    kept_id = doc["events"][0]["id"]
    doc["events"].append({"date": "2026-08-05", "who": "family", "icon": "star", "label": "FreshAdd"})
    client.post("/events", json=doc)
    after = client.get("/events").json()["events"]
    by_label = {e["label"]: e for e in after}
    assert by_label["FreshAdd"]["id"]                       # new item got an id
    assert any(e["id"] == kept_id for e in after)           # existing id preserved


def test_add_routes_assign_ids(client):
    client.post("/events/add", json={"date": "2026-08-06", "who": "family",
                                     "icon": "star", "label": "RouteAdd"})
    ev = next(e for e in client.get("/events").json()["events"] if e["label"] == "RouteAdd")
    assert len(ev["id"]) == 8


def test_delete_by_id(client):
    client.post("/events/add", json={"date": "2026-08-07", "who": "family",
                                     "icon": "star", "label": "DelById"})
    ev = next(e for e in client.get("/events").json()["events"] if e["label"] == "DelById")
    r = client.delete(f"/events/by-id/{ev['id']}")
    assert r.status_code == 200
    assert all(e["id"] != ev["id"] for e in client.get("/events").json()["events"])


def test_delete_by_id_404_on_unknown(client):
    assert client.delete("/events/by-id/deadbeef").status_code == 404


def test_relay_delete_prefers_id():
    import asyncio
    main.write(main.F_EVENTS, {"events": [
        {"id": "aaaa0001", "date": "2026-08-08", "who": "family", "icon": "star", "label": "Twin"},
        {"id": "aaaa0002", "date": "2026-08-08", "who": "family", "icon": "star", "label": "Twin"},
    ], "birthdays": [], "recurring": []})
    removed = asyncio.run(main._delete_event({"id": "aaaa0002"}))
    assert removed
    left = main.read_events()["events"]
    assert [e["id"] for e in left] == ["aaaa0001"]   # the *right* twin survived
