"""Provider-agnostic LLM layer. One place decides which model + provider every
AI feature uses (meals, recipes, shopping). The household picks two models, not one:
`settings.ai.visionModel` for requests that carry an image and `settings.ai.model` for
everything else — routed automatically from each request's own content, so a new
image-sending caller cannot be pointed at a text-only model by forgetting a flag.
Anthropic, OpenAI and Mistral are supported directly, plus
OpenRouter as a gateway to everything else; the model registry below (name/version
+ relative cost tier + `vision`) is the single source of truth for the admin pickers.

Model list current as of 2026-08 — Claude figures from the claude-api reference,
OpenAI figures from platform.openai.com pricing, Mistral from mistral.ai/pricing/api,
OpenRouter from its /api/v1/models listing.
`cost` is a 1–4 relative tier (rendered as $–$$$$), not a price."""
import os

import httpx
from fastapi import HTTPException

import ai_usage
from activity_log import log_event
from config import AI_MODEL, ANTHROPIC_API_KEY
from storage import read_settings

OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()
MISTRAL_API_KEY    = os.getenv("MISTRAL_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

PROVIDERS = ("anthropic", "openai", "mistral", "openrouter")

# provider, label (name + version), relative cost tier (1=cheapest … 4=priciest),
# `vision` (accepts image input), and optional `recVision` / `recText` — a short
# "why this one" shown in each picker's Recommended group.
#
# TWO ROLES, ONE REGISTRY. The household picks a model per role (see selected_model):
#   vision — anything that sends an image: recipe photo scans, Notion screenshot pages.
#   text   — everything else: the weekly plan, recipe text/link import, the meal-kit
#            split, plus cheap high-volume jobs (aisle tagging, unit normalization).
# INVARIANT: a row may serve the vision role only if `vision` is True. That used to be
# a rule about the whole registry; it is now structural — selected_model refuses to
# return a non-vision row for the vision role, whatever settings say — which is what
# lets text-only models live here at all.
# Frontier/overkill tiers (e.g. Claude Fable 5, GPT-5 pro) are deliberately left
# out: meal planning + recipe reading don't need them.
# Mistral ids use the `-latest` aliases (mistral-large-latest → Large 3 today),
# so the picker follows Mistral's own version rollovers.
# OpenRouter is a gateway, not a lab: one key reaches every lab's models, billed
# in one place. Its entries are therefore NOT a second copy of the catalogue —
# they're the models a household can't otherwise reach (Qwen, DeepSeek), plus the
# app's own default so an OpenRouter-only household still gets it. Its ids are
# `vendor/model` and are pinned by name, not `-latest`, because OpenRouter keeps old
# ids working.
# Re-verified against OpenRouter's /api/v1/models listing on 2026-08-20:
#   - DeepSeek publishes nothing multimodal (all 13 ids are text-in only). Before the
#     role split that disqualified it outright; now it is admitted for `text` alone,
#     where v4-flash is the cheapest option here by roughly 3x.
#   - Kimi/Moonshot IS multimodal from k2.5 onward, so the old "text-only" note was
#     wrong. It stays out for a different reason: k2-thinking spent its budget on
#     `reasoning` and returned empty `content` on a prompt this size, and the newer
#     rows are untested here. See extract_text() for what that failure looks like.
AI_MODELS = [
    {"id": "claude-haiku-4-5",     "provider": "anthropic", "label": "Claude Haiku 4.5",   "cost": 1,
     "vision": True, "recVision": "cheap, reads a recipe photo well",
     "recText": "cheap, and plans a week correctly"},
    {"id": "claude-sonnet-4-6",    "provider": "anthropic", "label": "Claude Sonnet 4.6",  "cost": 2,
     "vision": True, "recVision": "best all-round (the app default)",
     "recText": "best all-round (the app default)"},
    {"id": "claude-sonnet-5",      "provider": "anthropic", "label": "Claude Sonnet 5",    "cost": 2,
     "vision": True},
    {"id": "claude-opus-4-8",      "provider": "anthropic", "label": "Claude Opus 4.8",    "cost": 3,
     "vision": True},
    {"id": "gpt-5.4-nano",         "provider": "openai",    "label": "GPT-5.4 nano",       "cost": 1,
     "vision": True},
    {"id": "gpt-5.4-mini",         "provider": "openai",    "label": "GPT-5.4 mini",       "cost": 1,
     "vision": True, "recVision": "best value on an OpenAI key",
     "recText": "best value on an OpenAI key"},
    {"id": "gpt-5.4",              "provider": "openai",    "label": "GPT-5.4",            "cost": 2,
     "vision": True},
    {"id": "gpt-5.5",              "provider": "openai",    "label": "GPT-5.5",            "cost": 3,
     "vision": True},
    {"id": "ministral-14b-latest", "provider": "mistral",   "label": "Ministral 3 14B",    "cost": 1,
     "vision": True},
    {"id": "mistral-small-latest", "provider": "mistral",   "label": "Mistral Small 4",    "cost": 1,
     "vision": True},
    {"id": "mistral-large-latest", "provider": "mistral",   "label": "Mistral Large 3",    "cost": 1,
     "vision": True, "recVision": "most capability for the money",
     "recText": "most capability for the money"},
    {"id": "mistral-medium-latest", "provider": "mistral",  "label": "Mistral Medium 3.5", "cost": 2,
     "vision": True},
    {"id": "anthropic/claude-sonnet-4.6", "provider": "openrouter",
     "label": "Claude Sonnet 4.6 (OpenRouter)",     "cost": 2, "vision": True,
     "recVision": "the app default, on one shared key",
     "recText": "the app default, on one shared key"},
    {"id": "qwen/qwen3-vl-30b-a3b-instruct", "provider": "openrouter",
     "label": "Qwen3 VL 30B (OpenRouter)",          "cost": 1, "vision": True},
    {"id": "qwen/qwen3-vl-235b-a22b-instruct", "provider": "openrouter",
     "label": "Qwen3 VL 235B (OpenRouter)",         "cost": 1, "vision": True,
     "recVision": "cheapest that reads a photo cleanly",
     "recText": "cheap, and plans a week correctly"},
    # Dense 27B, where the 30B-A3B above activates only ~3B parameters per token.
    # Measured 2026-08-20 on a real recipe photo, and NOT recommended on the strength
    # of it: it does read a dense sheet well (14 ingredients / 5 steps vs the 235B's
    # 13 / 4) but costs 3.5x the 235B for the same photo, and it still missed the
    # planner's "skip dinner only when the event covers dinner" conditional, so it
    # gets no recText either. It also thinks by default — see the reasoning switch
    # in build_body, without which this row returns empty content and 502s.
    {"id": "qwen/qwen3.8-27b", "provider": "openrouter",
     "label": "Qwen3.8 27B (OpenRouter)",           "cost": 2, "vision": True},
    # Text-only: it can never be selected for photos, and selected_model enforces that.
    # Measured 2026-08-20 against the same planner prompt: the only model tried that
    # both skipped dinner for "Dinner with Anna" AND kept it for a swimming lesson,
    # while hitting the weekday protein spread — at ~1/2 the 30B's cost and 1/12th
    # the 27B's. That conditional is the one the household actually reported broken.
    {"id": "deepseek/deepseek-v4-flash", "provider": "openrouter",
     "label": "DeepSeek V4 Flash (OpenRouter)",     "cost": 1, "vision": False,
     "recText": "cheapest; best at the event rules"},
]
_BY_ID = {m["id"]: m for m in AI_MODELS}
DEFAULT_MODEL_ID = AI_MODEL   # the app's historical default (Claude Sonnet 4.6)
ROLES = ("vision", "text")


def _has_image(messages: list) -> bool:
    """Does this request carry an image? Reads the Anthropic-shaped content blocks the
    callers build, before build_body translates them to each provider's format."""
    return any(isinstance(m.get("content"), list)
               and any(isinstance(b, dict) and b.get("type") == "image" for b in m["content"])
               for m in (messages or []))


def selected_model(role: str = "text") -> dict:
    """The model the household picked for `role`.

    `model` holds the text role and `visionModel` the vision one; a settings file
    written before the split has only `model`, so both roles resolve to it and
    behaviour is unchanged until someone picks otherwise.

    A vision request is never returned a text-only row, whatever settings say — an
    unset, unknown, or text-only `visionModel` falls back to the app default. That
    is what makes the multimodal invariant structural instead of a rule to remember."""
    cfg     = read_settings().get("ai") or {}
    text_id = cfg.get("model") or DEFAULT_MODEL_ID
    mid     = (cfg.get("visionModel") or text_id) if role == "vision" else text_id
    model   = _BY_ID.get(mid) or _BY_ID.get(text_id) or _BY_ID.get(DEFAULT_MODEL_ID) or AI_MODELS[0]
    if role == "vision" and not model.get("vision"):
        model = _BY_ID.get(DEFAULT_MODEL_ID) or next(m for m in AI_MODELS if m.get("vision"))
    return model


def role_for(messages: list) -> str:
    return "vision" if _has_image(messages) else "text"


def model_for(messages: list) -> dict:
    """The model this particular request should go to, chosen by its own content."""
    return selected_model(role_for(messages))


def selected_models() -> dict:
    """Both roles at once, for the admin picker and the startup log."""
    return {r: selected_model(r)["id"] for r in ROLES}


def _api_key(provider: str) -> str:
    """Read at call time (not import time) so the module globals stay the single
    place a key lives — and stay patchable in tests."""
    return {"openai": OPENAI_API_KEY, "mistral": MISTRAL_API_KEY,
            "openrouter": OPENROUTER_API_KEY}.get(provider, ANTHROPIC_API_KEY)


def _key_env(provider: str) -> str:
    return {"openai": "OPENAI_API_KEY", "mistral": "MISTRAL_API_KEY",
            "openrouter": "OPENROUTER_API_KEY"}.get(provider, "ANTHROPIC_API_KEY")


def provider_ready(provider: str) -> bool:
    return bool(_api_key(provider))


class ProviderNotConfigured(HTTPException):
    """No API key for the selected model's provider. Distinct from a call that
    failed, because nothing was sent and nothing was billed — the relay drain
    uses that difference to decide whether a retry cost anything."""


def ensure_ready(model: dict | None = None, *, role: str = "text") -> dict:
    """Raise 500 if the model for `role` has no API key configured. Routes call this
    up front as a fail-fast guard; complete() calls it again with the role the request
    actually turns out to need."""
    model = model or selected_model(role)
    if not provider_ready(model["provider"]):
        raise ProviderNotConfigured(500, f"{_key_env(model['provider'])} not set")
    return model


def providers_status() -> dict:
    return {p: provider_ready(p) for p in PROVIDERS}


# ── Provider request/response shaping ─────────────────────────────────────────
# Everything except Anthropic speaks the OpenAI Chat Completions shape with bearer
# auth; only the host (and two body details, see build_body) differ.
CHAT_COMPLETIONS_URL = {
    "openai":     "https://api.openai.com/v1/chat/completions",
    "mistral":    "https://api.mistral.ai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}


def _endpoint(model: dict):
    provider = model["provider"]
    url = CHAT_COMPLETIONS_URL.get(provider)
    if url:
        headers = {"Authorization": f"Bearer {_api_key(provider)}", "content-type": "application/json"}
        if provider == "openrouter":
            # Attribution OpenRouter asks callers to send; affects only how the
            # request is labelled on their dashboard, never routing or billing.
            headers["X-Title"] = "Family Calendar"
        return (url, headers)
    return ("https://api.anthropic.com/v1/messages",
            {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})


def _openai_messages(system: str, messages: list, *, image_as_url_string: bool = False) -> list:
    """Translate the Anthropic-shaped system+messages (text and base64 image
    blocks) into OpenAI Chat Completions messages. Mistral takes the same shape
    except `image_url` is the data URL itself, not an {"url": …} object."""
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        parts = []
        for block in content or []:
            if block.get("type") == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                src = block.get("source", {})
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type', 'image/jpeg')};base64,{src.get('data', '')}"
                    parts.append({"type": "image_url",
                                  "image_url": url if image_as_url_string else {"url": url}})
        out.append({"role": m["role"], "content": parts})
    return out


def build_body(model: dict, system: str, messages: list, max_tokens: int) -> dict:
    provider = model["provider"]
    if provider not in CHAT_COMPLETIONS_URL:
        return {"model": model["id"], "max_tokens": max_tokens, "system": system, "messages": messages}
    # The OpenAI-shaped providers differ in exactly two details:
    #   - GPT-5 family wants `max_completion_tokens` (not `max_tokens`); no temperature.
    #     OpenRouter normalizes on `max_tokens` for every model it fronts, GPT-5 included.
    #   - Mistral wants `image_url` to be the data URL string, not an {"url": …} object.
    tokens_key = "max_completion_tokens" if provider == "openai" else "max_tokens"
    body = {"model": model["id"], tokens_key: max_tokens,
            "messages": _openai_messages(system, messages, image_as_url_string=provider == "mistral")}
    if provider == "openrouter":
        # Reasoning tokens are billed as output and share the max_tokens budget with
        # the answer — and every call this app makes wants strict JSON, never a
        # rationale. Measured on a real recipe photo: qwen3.8-27b thinks by default
        # and spent the entire 4000-token cap on `reasoning`, returning content=""
        # and finish_reason=length. extract_text() then yields "" and the route 502s,
        # which is the same failure that keeps Kimi out of the registry. Turning
        # reasoning off returns a clean 2040-token recipe for 40% less.
        # Verified accepted (content returned, zero reasoning tokens) by every
        # OpenRouter row offered here, thinking model or not.
        body["reasoning"] = {"enabled": False}
    return body


def extract_text(provider: str, result: dict) -> str:
    if provider in CHAT_COMPLETIONS_URL:
        content = ((result.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(content, list):
            # Mistral's hybrid/reasoning models answer in chunks (thinking + text);
            # only the text chunks are the answer.
            return "".join(c.get("text", "") for c in content if c.get("type") == "text")
        return content or ""
    return "".join(b["text"] for b in result.get("content", []) if b.get("type") == "text")


def _error_message(result: dict) -> str:
    """Human-readable error from a provider error body — Anthropic/OpenAI/OpenRouter
    use {"error": {"message": ...}}, Mistral a top-level "message" or "detail";
    truncated so it's safe to surface/log."""
    err = result.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)[:300]
    return str(err or result.get("message") or result.get("detail") or result)[:300]


def _finish_reason(result: dict) -> str:
    return ((result.get("choices") or [{}])[0].get("finish_reason")
            or result.get("stop_reason") or "")


def _failed(status: int, result: dict) -> bool:
    """A non-200, or a 200 carrying an error body — OpenRouter answers 200 with
    {"error": …} when the upstream provider it routed to fails, so status alone
    isn't enough to tell a completion from a failure."""
    return status != 200 or bool(isinstance(result, dict) and result.get("error"))


async def complete(system: str, messages: list, max_tokens: int,
                   *, action: str, timeout: int = 45) -> str:
    """Run one completion against the selected provider and return the text.
    Raises HTTPException (with logging) on missing key, unreachable service, or an
    error response — the same failure contract every AI route already relied on."""
    model = ensure_ready(role=role_for(messages))
    url, headers = _endpoint(model)
    body = build_body(model, system, messages, max_tokens)
    tag = {"model": model["id"], "provider": model["provider"]}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        log_event("connectivity", action, f"Could not reach the AI service ({model['id']}): {e}",
                  level="error", detail=tag)
        raise HTTPException(502, "Could not reach the AI service")
    result = resp.json()
    if _failed(resp.status_code, result):
        msg = _error_message(result)
        log_event("ai", action, f"AI service error {resp.status_code} ({model['id']}): {msg}",
                  level="error", detail={**tag, "status": resp.status_code, "response": result})
        raise HTTPException(502, f"AI error from {model['id']}: {msg}")   # surface the real cause
    ai_usage.record(model, action, result)
    if _finish_reason(result) in ("length", "max_tokens"):
        # The reply was cut at the token cap, so JSON output is truncated and will
        # fail to parse. Say so here — otherwise the only symptom is a baffling
        # "Expecting value: line 350" from the parser, with no hint of the cause.
        log_event("ai", action, f"AI reply hit the {max_tokens}-token cap and was cut off "
                                f"({model['id']}) — the result is likely unusable",
                  level="warn", detail={**tag, "max_tokens": max_tokens})
    text = extract_text(model["provider"], result)
    if not text.strip():
        log_event("ai", action, f"AI returned no text ({model['id']})", level="error",
                  detail={**tag, "finish_reason": _finish_reason(result)})
        raise HTTPException(502, f"{model['id']} returned an empty response "
                                 f"(finish_reason={_finish_reason(result) or 'unknown'})")
    return text


async def complete_or_none(system: str, messages: list, max_tokens: int,
                           *, action: str, timeout: int = 45):
    """Best-effort variant for background features (e.g. aisle tagging): returns
    the text, or None on any failure — missing key, unreachable, error body — after
    logging a warning. Never raises, so the caller can fall back gracefully."""
    model = model_for(messages)
    if not provider_ready(model["provider"]):
        return None
    url, headers = _endpoint(model)
    body = build_body(model, system, messages, max_tokens)
    tag = {"model": model["id"], "provider": model["provider"]}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
        data = resp.json()
        if _failed(resp.status_code, data):
            log_event("ai", action,
                      f"AI service error {resp.status_code} ({model['id']}): {_error_message(data)}, using fallback",
                      level="warn", detail={**tag, "status": resp.status_code})
            return None
        ai_usage.record(model, action, data)
        return extract_text(model["provider"], data)
    except Exception as e:
        log_event("ai", action, f"AI call failed ({model['id']}), using fallback: {e}",
                  level="warn", detail=tag)
        return None
