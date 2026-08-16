"""The model picker's registry is hand-maintained, and a bad row is only felt
later — at the provider, in a feature that happens to send an image. The
invariant that matters most: every selectable model must accept image input,
because recipe photo extraction sends one and a text-only model fails there
while looking fine everywhere else."""
import ai
import pytest

# Verified against OpenRouter's /api/v1/models listing (input_modalities) and
# each lab's own model pages. A new row belongs here only once it is checked.
KNOWN_TEXT_ONLY_ON_OPENROUTER = ("deepseek/", "moonshotai/kimi-k2-", "mistralai/mistral-7b")


def test_every_model_row_is_complete():
    for m in ai.AI_MODELS:
        assert set(m) <= {"id", "provider", "label", "cost", "rec"}, m
        assert m["id"] and m["label"], m
        assert m["provider"] in ai.PROVIDERS, m
        assert m["cost"] in (1, 2, 3, 4), m


def test_ids_are_unique():
    ids = [m["id"] for m in ai.AI_MODELS]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_lookup_covers_every_row_and_the_default():
    assert set(ai._BY_ID) == {m["id"] for m in ai.AI_MODELS}
    assert ai.DEFAULT_MODEL_ID in ai._BY_ID


def test_no_known_text_only_model_is_selectable():
    """Guards the multimodal invariant against the tempting cheap additions:
    DeepSeek publishes nothing image-capable on OpenRouter."""
    for m in ai.AI_MODELS:
        assert not m["id"].startswith(KNOWN_TEXT_ONLY_ON_OPENROUTER), \
            f"{m['id']} cannot read a recipe photo — see the invariant in ai.py"


def test_every_provider_can_be_selected_from():
    """Each provider a household might hold a key for needs at least one row,
    or that key buys them nothing."""
    covered = {m["provider"] for m in ai.AI_MODELS}
    assert covered == set(ai.PROVIDERS), set(ai.PROVIDERS) - covered


@pytest.mark.parametrize("provider", ai.PROVIDERS)
def test_every_provider_recommends_something(provider):
    """A household holding just one key should still be told where to start.
    Several per provider is fine — they answer different questions (cheapest
    that works, best all-round) — but none leaves that key unguided."""
    assert [m for m in ai.AI_MODELS if m["provider"] == provider and m.get("rec")]
