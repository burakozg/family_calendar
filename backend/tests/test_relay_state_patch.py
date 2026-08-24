"""Relay-side check-off state (shopping-relay/main.py).

Loaded by path under a distinct module name so it does not shadow the backend's
own `main`. It lives here so ./run-tests.sh covers the relay as well — a relay
bug is what wiped a real shopping list, and the relay had no tests at all.

The defect: /state assigned BOTH `have` and `bought` from the request body with an
`or []` fallback, while the phone only ever sends `bought` (app.js has no `have:`
key anywhere). So every single tick reset "already have at home" to empty.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RELAY_SRC = Path(__file__).resolve().parents[2] / "shopping-relay" / "main.py"


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    data = tmp_path_factory.mktemp("relay") / "relay.json"
    os.environ.update({"PUBLISH_TOKEN": "pub", "DEVICE_TOKEN": "dev",
                       "RELAY_DATA": str(data)})
    spec = importlib.util.spec_from_file_location("relay_app", RELAY_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_app"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def c(relay):
    relay.write_store(relay._blank_store())
    return TestClient(relay.app)


DEV = {"Authorization": "Bearer dev"}
PUB = {"Authorization": "Bearer pub"}


def _publish(c, bought_survives=("tomato",)):
    c.post("/publish", headers=PUB, json={
        "week": "2026-34", "start": "2026-08-23",
        "days": [{"day": "Mon", "date": "2026-08-24", "name": "Stew",
                  "ingredients": [{"item": i} for i in bought_survives]}],
        "have": ["salt"], "extras": []})


def test_a_tick_does_not_wipe_the_have_list(c):
    """The actual data-loss bug. The phone sends only `bought`."""
    _publish(c)
    assert c.get("/list", headers=DEV).json()["have"] == ["salt"]

    c.post("/state", headers=DEV, json={"bought": ["tomato"]})

    s = c.get("/list", headers=DEV).json()
    assert s["bought"] == ["tomato"]
    assert s["have"] == ["salt"], "ticking an item erased the at-home list"


def test_an_absent_key_means_unchanged_not_empty(c):
    _publish(c)
    c.post("/state", headers=DEV, json={"have": ["salt", "oil"], "bought": ["tomato"]})
    c.post("/state", headers=DEV, json={"bought": []})          # untick everything

    s = c.get("/list", headers=DEV).json()
    assert s["bought"] == []                                     # explicit → applied
    assert s["have"] == ["salt", "oil"]                          # absent → untouched


def test_an_explicit_empty_list_still_clears(c):
    """Patch semantics must not make a genuine clear impossible."""
    _publish(c)
    c.post("/state", headers=DEV, json={"have": ["salt"], "bought": ["tomato"]})
    c.post("/state", headers=DEV, json={"have": []})

    s = c.get("/list", headers=DEV).json()
    assert s["have"] == []
    assert s["bought"] == ["tomato"]


def test_publish_carries_ticks_forward_and_prunes_only_what_left(c):
    _publish(c, bought_survives=("tomato", "onion"))
    c.post("/state", headers=DEV, json={"bought": ["tomato", "onion"]})

    _publish(c, bought_survives=("tomato",))                     # onion rolled off

    assert c.get("/list", headers=DEV).json()["bought"] == ["tomato"]


def test_state_requires_the_device_token(c):
    assert c.post("/state", json={"bought": []}).status_code in (401, 403)
    assert c.post("/state", headers=PUB, json={"bought": []}).status_code in (401, 403)
