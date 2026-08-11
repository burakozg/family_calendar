"""Daily AI token accounting: parsing each provider's `usage` block, the
append-only ledger, and the per-day/per-model rollup behind /ai/usage."""
import json
from datetime import datetime, timedelta

import ai_usage
import pytest


@pytest.fixture(autouse=True)
def clean_ledger():
    """Every test starts from an empty ledger — it is a file, not a fixture."""
    if ai_usage.USAGE_FILE.exists():
        ai_usage.USAGE_FILE.unlink()
    yield
    if ai_usage.USAGE_FILE.exists():
        ai_usage.USAGE_FILE.unlink()


# Response shapes below are trimmed copies of real replies from each provider.
def test_usage_parsed_per_provider():
    assert ai_usage._usage_from("anthropic", {"usage": {
        "input_tokens": 13, "output_tokens": 4}}) == (13, 4, None)
    assert ai_usage._usage_from("openai", {"usage": {
        "prompt_tokens": 100, "completion_tokens": 20}}) == (100, 20, None)
    assert ai_usage._usage_from("openrouter", {"usage": {
        "prompt_tokens": 5, "completion_tokens": 1, "cost": 4e-06}}) == (5, 1, 4e-06)
    assert ai_usage._usage_from("anthropic", {}) == (0, 0, None)      # tolerant of empties


def test_anthropic_cache_tokens_count_as_input():
    """Cache reads/writes are billed input; the app sends none today, but the
    number must stay true if it ever starts."""
    assert ai_usage._usage_from("anthropic", {"usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 3, "output_tokens": 2}}) == (18, 2, None)


def test_record_appends_and_skips_unmetered_calls():
    m = {"id": "claude-haiku-4-5", "provider": "anthropic"}
    ai_usage.record(m, "meals.suggest", {"usage": {"input_tokens": 30, "output_tokens": 7}})
    ai_usage.record(m, "meals.suggest", {})                            # no usage — not a row
    ai_usage.record(m, "meals.suggest", {"usage": {"input_tokens": 0, "output_tokens": 0}})
    lines = ai_usage.USAGE_FILE.read_text().splitlines()
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert (e["in"], e["out"], e["model"], e["action"]) == (30, 7, "claude-haiku-4-5", "meals.suggest")
    assert "cost" not in e                                             # anthropic reports none


def test_record_never_raises_on_a_junk_response():
    """Accounting must not be able to fail a feature that already answered."""
    ai_usage.record({"id": "x", "provider": "openai"}, "a", {"usage": "not-a-dict"})
    ai_usage.record({"id": "x", "provider": "openai"}, "a", None)
    assert not ai_usage.USAGE_FILE.exists()


def _write(entries):
    ai_usage.USAGE_FILE.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_summary_buckets_by_local_day():
    today = datetime.now()
    _write([
        {"ts": today.isoformat(timespec="seconds"), "model": "m1", "provider": "openrouter",
         "action": "a", "in": 100, "out": 10, "cost": 0.002},
        {"ts": today.isoformat(timespec="seconds"), "model": "m1", "provider": "openrouter",
         "action": "a", "in": 50, "out": 5, "cost": 0.001},
        {"ts": (today - timedelta(days=2)).isoformat(timespec="seconds"), "model": "m2",
         "provider": "anthropic", "action": "b", "in": 7, "out": 3},
        {"ts": (today - timedelta(days=40)).isoformat(timespec="seconds"), "model": "m2",
         "provider": "anthropic", "action": "b", "in": 999, "out": 999},   # outside the window
    ])
    s = ai_usage.summary(days=7)
    assert len(s["days"]) == 7 and s["days"][0]["date"] == today.date().isoformat()
    assert s["today"]["calls"] == 2 and s["today"]["total"] == 165
    assert s["today"]["cost"] == 0.003 and s["today"]["cost_known"]
    assert s["days"][2]["total"] == 10                                 # two days back
    assert s["window"]["calls"] == 3 and s["window"]["total"] == 175   # the 40-day-old row is excluded
    assert [m["model"] for m in s["models"]] == ["m1", "m2"]           # biggest consumer first


def test_cost_is_marked_a_floor_when_a_provider_reports_none():
    """A window mixing OpenRouter (reports cost) with Anthropic (doesn't) must not
    present the partial sum as the full spend."""
    now = datetime.now().isoformat(timespec="seconds")
    _write([
        {"ts": now, "model": "or", "provider": "openrouter", "action": "a", "in": 1, "out": 1, "cost": 0.5},
        {"ts": now, "model": "cl", "provider": "anthropic", "action": "a", "in": 1, "out": 1},
    ])
    s = ai_usage.summary(days=7)
    assert s["today"]["cost"] == 0.5 and not s["today"]["cost_known"]
    by_model = {m["model"]: m for m in s["models"]}
    assert by_model["or"]["cost_known"] and not by_model["cl"]["cost_known"]


def test_summary_route(client):
    _write([{"ts": datetime.now().isoformat(timespec="seconds"), "model": "m", "provider": "openai",
             "action": "a", "in": 4, "out": 2}])
    r = client.get("/ai/usage?days=3").json()
    assert len(r["days"]) == 3 and r["today"]["total"] == 6
    assert client.get("/ai/usage?days=9999").json()["days"]            # clamped, not a crash
