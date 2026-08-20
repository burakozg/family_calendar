"""Provider-agnostic AI layer: model registry, per-role selection from settings,
role routing by request content, provider readiness, and Anthropic/OpenAI/Mistral/
OpenRouter request/response shaping."""
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
        assert isinstance(m["vision"], bool)


def test_every_provider_has_a_recommendation():
    """Each picker's Recommended group must cover every provider — a household on
    a single API key should always find a flagged option, in both roles."""
    for key in ("recVision", "recText"):
        rec = [m for m in ai.AI_MODELS if m.get(key)]
        assert {m["provider"] for m in rec} == set(ai.PROVIDERS), key
        assert ai.DEFAULT_MODEL_ID in {m["id"] for m in rec}, key      # the default is one of them
        for m in rec:
            assert len(m[key]) <= 40, m                                # readable in an <option>


@pytest.fixture(autouse=True)
def _restore_ai_settings():
    """Tests here rewrite settings.ai. Leaving a pick behind changes which provider
    key later modules expect to be missing — it broke test_api's fail-closed check
    exactly that way — so put the block back on the way out."""
    before = main.read_settings().get("ai")
    yield
    main.write(main.F_SETTINGS, {**main.read_settings(), "ai": before})


def _set_ai(**cfg):
    main.write(main.F_SETTINGS, {**main.read_settings(), "ai": cfg})


def test_selected_model_default_and_override():
    _set_ai()
    assert ai.selected_model()["id"] == "claude-sonnet-4-6"           # default
    _set_ai(model="gpt-5.4")
    assert ai.selected_model()["id"] == "gpt-5.4"
    _set_ai(model="nope")
    assert ai.selected_model()["id"] == "claude-sonnet-4-6"           # unknown → default


def test_pre_split_settings_drive_both_roles():
    """What the NAS actually has on disk today: an `ai` block with only `model`.
    Both roles must resolve to it, so upgrading changes nothing until someone picks."""
    _set_ai(model="gpt-5.4")
    assert ai.selected_model("text")["id"] == "gpt-5.4"
    assert ai.selected_model("vision")["id"] == "gpt-5.4"
    assert ai.selected_models() == {"vision": "gpt-5.4", "text": "gpt-5.4"}


def test_roles_select_independently():
    _set_ai(model="deepseek/deepseek-v4-flash", visionModel="qwen/qwen3.8-27b")
    assert ai.selected_model("text")["id"] == "deepseek/deepseek-v4-flash"
    assert ai.selected_model("vision")["id"] == "qwen/qwen3.8-27b"


def test_vision_role_never_returns_a_text_only_model():
    """The invariant that used to cover the whole registry. A text-only pick for the
    text role must not drag photos down with it — however settings got that way."""
    _set_ai(model="deepseek/deepseek-v4-flash")                       # no visionModel at all
    assert ai.selected_model("vision")["vision"] is True
    _set_ai(model="claude-sonnet-4-6", visionModel="deepseek/deepseek-v4-flash")
    assert ai.selected_model("vision")["vision"] is True              # explicitly text-only → refused
    _set_ai(model="claude-sonnet-4-6", visionModel="nope")
    assert ai.selected_model("vision")["id"] == "claude-sonnet-4-6"   # unknown → falls back


def test_role_is_routed_from_request_content():
    text_msgs  = [{"role": "user", "content": "just words"}]
    block_msgs = [{"role": "user", "content": [{"type": "text", "text": "Extract this."}]}]
    img_msgs   = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}},
        {"type": "text", "text": "Extract this."}]}]
    assert ai.role_for(text_msgs) == "text"
    assert ai.role_for(block_msgs) == "text"                          # blocks without an image
    assert ai.role_for(img_msgs) == "vision"
    assert ai.role_for([]) == "text"
    _set_ai(model="deepseek/deepseek-v4-flash", visionModel="qwen/qwen3.8-27b")
    assert ai.model_for(text_msgs)["id"] == "deepseek/deepseek-v4-flash"
    assert ai.model_for(img_msgs)["id"] == "qwen/qwen3.8-27b"


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


def test_only_openrouter_gets_the_reasoning_switch():
    """Reasoning tokens share the max_tokens budget with the answer, and every call
    this app makes wants strict JSON. Measured: qwen3.8-27b spent a whole 4000-token
    cap thinking and returned content="", which surfaces as a 502. Only OpenRouter
    accepts this field — sending it to Anthropic/OpenAI/Mistral would be a 400."""
    messages = [{"role": "user", "content": "hi"}]
    for provider, mid in (("anthropic", "claude-sonnet-4-6"), ("openai", "gpt-5.4"),
                          ("mistral", "mistral-large-latest")):
        body = ai.build_body({"id": mid, "provider": provider}, "SYS", messages, 100)
        assert "reasoning" not in body, provider
    body = ai.build_body({"id": "qwen/qwen3.8-27b", "provider": "openrouter"},
                         "SYS", messages, 100)
    assert body["reasoning"] == {"enabled": False}


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
    assert body["reasoning"] == {"enabled": False}                    # see below
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
    assert isinstance(r["models"], list)
    assert set(r["selected"]) == set(ai.ROLES)                        # one selection per picker
    assert all(r["selected"][role] for role in ai.ROLES)
    assert set(r["providers"]) == set(ai.PROVIDERS)


def test_complete_sends_the_role_s_model(monkeypatch):
    """The end of the chain: whatever selected_model decides has to reach the wire.
    Asserting on the request body is what catches a role resolved correctly and then
    dropped somewhere in build_body."""
    _set_ai(model="gpt-5.4", visionModel="claude-sonnet-4-6")
    sent = []

    class _Resp:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}],
                                "content": [{"type": "text", "text": "ok"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **k):
            sent.append(k["json"])
            return _Resp()

    monkeypatch.setattr(ai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "sk-openai")

    asyncio.run(ai.complete("s", [{"role": "user", "content": "plan the week"}],
                            100, action="meal.generate"))
    asyncio.run(ai.complete("s", [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}},
        {"type": "text", "text": "Extract this."}]}], 100, action="recipe.extract_photo"))

    assert [b["model"] for b in sent] == ["gpt-5.4", "claude-sonnet-4-6"]


def test_complete_or_none_routes_by_role_too(monkeypatch):
    """Background jobs go through the other entry point; it must route identically."""
    _set_ai(model="gpt-5.4", visionModel="claude-sonnet-4-6")
    sent = []

    class _Resp:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **k):
            sent.append(k["json"])
            return _Resp()

    monkeypatch.setattr(ai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "sk-openai")
    asyncio.run(ai.complete_or_none("s", [{"role": "user", "content": "tag these"}],
                                    100, action="shopping.categorize"))
    assert sent[0]["model"] == "gpt-5.4"
