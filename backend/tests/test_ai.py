"""Provider-agnostic AI layer: model registry, selection from settings, provider
readiness, and Anthropic/OpenAI/Mistral/OpenRouter request/response shaping."""
import asyncio

import ai
import main
import pytest
from fastapi import HTTPException


def test_registry_shape():
    ids = {m["id"] for m in ai.AI_MODELS}
    assert "claude-sonnet-4-6" in ids
    providers = {m["provider"] for m in ai.AI_MODELS}
    assert providers == set(ai.PROVIDERS)                              # every provider is offered
    for m in ai.AI_MODELS:
        assert m["provider"] in ai.PROVIDERS
        assert 1 <= m["cost"] <= 4 and m["label"]


def test_every_provider_has_a_recommendation():
    """The picker's Recommended group must cover every provider — a household on
    a single API key should always find a flagged option."""
    rec = [m for m in ai.AI_MODELS if m.get("rec")]
    assert {m["provider"] for m in rec} == set(ai.PROVIDERS)
    assert ai.DEFAULT_MODEL_ID in {m["id"] for m in rec}               # the default is one of them
    for m in rec:
        assert len(m["rec"]) <= 40                                     # stays readable in an <option>


def test_selected_model_default_and_override():
    main.write(main.F_SETTINGS, {**main.read_settings(), "ai": {}})
    assert ai.selected_model()["id"] == "claude-sonnet-4-6"           # default
    main.write(main.F_SETTINGS, {**main.read_settings(), "ai": {"model": "gpt-5.4"}})
    assert ai.selected_model()["id"] == "gpt-5.4"
    main.write(main.F_SETTINGS, {**main.read_settings(), "ai": {"model": "nope"}})
    assert ai.selected_model()["id"] == "claude-sonnet-4-6"           # unknown → default


def test_ensure_ready_gated_on_provider_key(monkeypatch):
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "")
    monkeypatch.setattr(ai, "MISTRAL_API_KEY", "")
    monkeypatch.setattr(ai, "OPENROUTER_API_KEY", "")
    with pytest.raises(HTTPException) as e:                            # claude default, no key
        ai.ensure_ready({"id": "claude-sonnet-4-6", "provider": "anthropic"})
    assert e.value.status_code == 500 and "ANTHROPIC_API_KEY" in e.value.detail
    with pytest.raises(HTTPException) as e:
        ai.ensure_ready({"id": "mistral-large-latest", "provider": "mistral"})
    assert "MISTRAL_API_KEY" in e.value.detail                         # names the right env var
    with pytest.raises(HTTPException) as e:
        ai.ensure_ready({"id": "x-ai/grok-4.5", "provider": "openrouter"})
    assert "OPENROUTER_API_KEY" in e.value.detail
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "sk-openai")
    monkeypatch.setattr(ai, "MISTRAL_API_KEY", "sk-mistral")
    monkeypatch.setattr(ai, "OPENROUTER_API_KEY", "sk-or-key")
    assert ai.provider_ready("openai") and ai.provider_ready("mistral")
    assert ai.provider_ready("openrouter") and not ai.provider_ready("anthropic")
    ai.ensure_ready({"id": "gpt-5.4", "provider": "openai"})           # openai now ready
    ai.ensure_ready({"id": "mistral-large-latest", "provider": "mistral"})
    ai.ensure_ready({"id": "x-ai/grok-4.5", "provider": "openrouter"})


def test_build_body_anthropic_unchanged():
    m = {"id": "claude-sonnet-4-6", "provider": "anthropic"}
    body = ai.build_body(m, "SYS", [{"role": "user", "content": "hi"}], 1500)
    assert body == {"model": "claude-sonnet-4-6", "max_tokens": 1500,
                    "system": "SYS", "messages": [{"role": "user", "content": "hi"}]}


def test_build_body_openai_translates_system_tokens_and_image():
    m = {"id": "gpt-5.4", "provider": "openai"}
    messages = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC"}},
        {"type": "text", "text": "Extract this recipe."},
    ]}]
    body = ai.build_body(m, "SYS", messages, 2000)
    assert body["model"] == "gpt-5.4"
    assert body["max_completion_tokens"] == 2000 and "max_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,ABC"}}
    assert parts[1] == {"type": "text", "text": "Extract this recipe."}


def test_build_body_mistral_uses_max_tokens_and_data_url_string():
    m = {"id": "mistral-large-latest", "provider": "mistral"}
    messages = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC"}},
        {"type": "text", "text": "Extract this recipe."},
    ]}]
    body = ai.build_body(m, "SYS", messages, 2000)
    assert body["model"] == "mistral-large-latest"
    assert body["max_tokens"] == 2000 and "system" not in body        # system is a message
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "image_url", "image_url": "data:image/png;base64,ABC"}
    assert parts[1] == {"type": "text", "text": "Extract this recipe."}


def test_build_body_openrouter_uses_max_tokens_and_url_object(monkeypatch):
    """OpenRouter fronts every lab behind the OpenAI shape and normalizes on
    `max_tokens` — even for GPT-5 models, which want max_completion_tokens direct."""
    m = {"id": "anthropic/claude-sonnet-4.6", "provider": "openrouter"}
    messages = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC"}},
        {"type": "text", "text": "Extract this recipe."},
    ]}]
    body = ai.build_body(m, "SYS", messages, 2000)
    assert body["model"] == "anthropic/claude-sonnet-4.6"
    assert body["max_tokens"] == 2000 and "max_completion_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC"}}

    monkeypatch.setattr(ai, "OPENROUTER_API_KEY", "sk-or-key")
    url, headers = ai._endpoint(m)
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-or-key" and headers["X-Title"]


def test_error_body_on_a_200_counts_as_failure():
    """OpenRouter answers 200 with {"error": …} when the provider it routed to
    fails — treating that as success would surface an empty plan instead."""
    body = {"error": {"message": "upstream is down"}}
    assert ai._failed(200, body) and ai._error_message(body) == "upstream is down"
    assert not ai._failed(200, {"choices": [{"message": {"content": "Hello"}}]})
    assert ai._failed(429, {})


def test_truncated_reply_is_logged_as_such(monkeypatch):
    """A reply cut at the token cap yields invalid JSON downstream. Without this
    warning the only symptom is 'Expecting value: line 350' from the parser, which
    says nothing about the real cause."""
    logged = []
    monkeypatch.setattr(ai, "log_event", lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(ai, "ensure_ready", lambda *a, **k: {"id": "m", "provider": "openai"})

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": '{"name": "Half a rec'}}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(ai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "sk-test")
    asyncio.run(ai.complete("s", [{"role": "user", "content": "hi"}], 2000, action="recipe.extract_text"))
    assert any("cap" in a[2] and "cut off" in a[2] for a, k in logged), logged


def test_provider_not_configured_is_its_own_error(monkeypatch):
    """The relay drain distinguishes 'no key, nothing billed' from a failed call."""
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ai.ProviderNotConfigured):
        ai.ensure_ready({"id": "claude-sonnet-4-6", "provider": "anthropic"})
    assert issubclass(ai.ProviderNotConfigured, HTTPException)         # still a 500 to routes


def test_extract_text_per_provider():
    assert ai.extract_text("anthropic", {"content": [
        {"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]}) == "Hello"
    assert ai.extract_text("openai", {"choices": [
        {"message": {"content": "Hello"}}]}) == "Hello"
    assert ai.extract_text("openai", {}) == ""                        # tolerant of empties
    assert ai.extract_text("mistral", {"choices": [
        {"message": {"content": "Hello"}}]}) == "Hello"
    assert ai.extract_text("mistral", {"choices": [{"message": {"content": [   # reasoning chunks
        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
        {"type": "text", "text": "Hello"}]}}]}) == "Hello"
    assert ai.extract_text("openrouter", {"choices": [
        {"message": {"content": "Hello"}}]}) == "Hello"


def test_ai_models_route(client):
    r = client.get("/ai/models").json()
    assert r["selected"] and isinstance(r["models"], list)
    assert set(r["providers"]) == set(ai.PROVIDERS)
