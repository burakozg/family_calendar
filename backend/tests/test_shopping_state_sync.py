"""Check-off state must survive the round trip to the phone.

Before this, `bought` had exactly one copy — on the cloud relay. publish() pushes
state outward only (it literally sends "bought": []), and nothing read it back, so
ticks made on the phone lived outside the NAS and outside the daily backup. When
they were lost there was no home copy to even compare against.
"""
import asyncio
from datetime import date

import relay_client
import shopping
import storage


class _ListClient:
    """httpx.AsyncClient stand-in serving a canned /list document."""
    doc: dict = {}
    status: int = 200
    calls: int = 0

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None):
        _ListClient.calls += 1
        class _R:
            status_code = _ListClient.status
            def json(self_inner): return _ListClient.doc
        return _R()


def _relay_on(monkeypatch, doc, status=200):
    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(relay_client, "SHOP_RELAY_PUBLISH_TOKEN", "tok")
    monkeypatch.setattr(shopping.httpx, "AsyncClient", _ListClient)
    _ListClient.doc, _ListClient.status, _ListClient.calls = doc, status, 0


def _week():
    return shopping._week_key_of(date.today())


def _doc(**over):
    d = {"week": _week(), "bought": ["tomato", "onion"], "have": [],
         "state_updated_at": "2026-08-22T13:35:35+00:00"}
    d.update(over)
    return d


def test_phone_ticks_are_recorded_on_the_nas(monkeypatch, client):
    storage.write_shopping({})
    _relay_on(monkeypatch, _doc())

    assert asyncio.run(shopping.pull_shopping_state()) is True
    entry = storage.read_shopping()[_week()]
    assert entry["bought"] == ["tomato", "onion"]
    assert entry["relay_state_at"] == "2026-08-22T13:35:35+00:00"


def test_the_same_state_is_not_rewritten_every_poll(monkeypatch, client):
    """The loop runs every ~90s; re-recording an unchanged state would churn the
    file forever and make its mtime useless as a signal."""
    storage.write_shopping({})
    _relay_on(monkeypatch, _doc())

    assert asyncio.run(shopping.pull_shopping_state()) is True
    assert asyncio.run(shopping.pull_shopping_state()) is False


def test_a_never_ticked_relay_cannot_blank_the_nas(monkeypatch, client):
    """A blank store — fresh volume, restored machine, error — reports no
    state_updated_at. Treating that as authoritative is how a single bad read
    becomes permanent data loss."""
    storage.write_shopping({_week(): {"bought": ["tomato"], "have": ["salt"]}})
    _relay_on(monkeypatch, _doc(bought=[], state_updated_at=""))

    assert asyncio.run(shopping.pull_shopping_state()) is False
    assert storage.read_shopping()[_week()]["bought"] == ["tomato"]


def test_a_genuine_clear_from_the_phone_is_honoured(monkeypatch, client):
    """The converse: unticking everything IS a real edit, and must land."""
    storage.write_shopping({_week(): {"bought": ["tomato"], "relay_state_at": "old"}})
    _relay_on(monkeypatch, _doc(bought=[], state_updated_at="2026-08-23T09:00:00+00:00"))

    assert asyncio.run(shopping.pull_shopping_state()) is True
    assert storage.read_shopping()[_week()]["bought"] == []


def test_pull_leaves_have_and_extras_alone(monkeypatch, client):
    """`have` is authored at home and the phone has no UI for it, so the relay's
    copy is only a mirror — writing it back could clobber a fresh home edit."""
    storage.write_shopping({_week(): {"have": ["salt", "oil"],
                                      "extras": [{"item": "Batteries", "who": ""}]}})
    _relay_on(monkeypatch, _doc(have=[]))

    assert asyncio.run(shopping.pull_shopping_state()) is True
    entry = storage.read_shopping()[_week()]
    assert entry["have"] == ["salt", "oil"]
    assert entry["extras"] == [{"item": "Batteries", "who": ""}]


def test_a_failing_relay_never_raises_or_writes(monkeypatch, client):
    storage.write_shopping({_week(): {"bought": ["tomato"]}})
    _relay_on(monkeypatch, {}, status=503)

    assert asyncio.run(shopping.pull_shopping_state()) is False
    assert storage.read_shopping()[_week()]["bought"] == ["tomato"]


def test_no_relay_configured_is_a_silent_noop(monkeypatch, client):
    monkeypatch.setattr(relay_client, "SHOP_RELAY_URL", "")
    assert asyncio.run(shopping.pull_shopping_state()) is False
