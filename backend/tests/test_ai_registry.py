"""The model picker's registry is hand-maintained, and a bad row is only felt
later — at the provider, in a feature that happens to send an image. The invariant
that matters most: a row may be offered for the vision role only if it accepts image
input. Text-only models are welcome here now that the roles are split, which makes
the `vision` flag load-bearing: get it wrong and recipe photo extraction fails while
everything else looks fine."""
import ai
import pytest

# Verified against OpenRouter's /api/v1/models listing (input_modalities) and
# each lab's own model pages. A new row belongs here only once it is checked.
KNOWN_TEXT_ONLY_ON_OPENROUTER = ("deepseek/", "moonshotai/kimi-k2-", "mistralai/mistral-7b")


def test_every_model_row_is_complete():
    for m in ai.AI_MODELS:
        assert set(m) <= {"id", "provider", "label", "cost", "vision",
                          "recVision", "recText"}, m
        assert m["id"] and m["label"], m
        assert m["provider"] in ai.PROVIDERS, m
        assert m["cost"] in (1, 2, 3, 4), m


def test_ids_are_unique():
    ids = [m["id"] for m in ai.AI_MODELS]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]


def test_lookup_covers_every_row_and_the_default():
    assert set(ai._BY_ID) == {m["id"] for m in ai.AI_MODELS}
    assert ai.DEFAULT_MODEL_ID in ai._BY_ID


def test_no_known_text_only_model_is_offered_for_vision():
    """DeepSeek publishes nothing image-capable on OpenRouter, so it may sit in the
    registry for the text role but must never be flagged vision-capable."""
    for m in ai.AI_MODELS:
        if m["id"].startswith(KNOWN_TEXT_ONLY_ON_OPENROUTER):
            assert m["vision"] is False, \
                f"{m['id']} cannot read a recipe photo — see the invariant in ai.py"


def test_vision_recommendation_implies_vision_capable():
    """The converse of the above, and the likelier mistake: a row picks up a
    recVision blurb without the flag, and the photo picker offers something the
    backend will then silently refuse."""
    for m in ai.AI_MODELS:
        if m.get("recVision"):
            assert m["vision"] is True, m


def test_both_roles_have_something_to_choose_from():
    assert [m for m in ai.AI_MODELS if m["vision"]]
    assert [m for m in ai.AI_MODELS if not m["vision"]], \
        "no text-only rows — the whole point of splitting the roles was to allow them"


def test_default_model_can_serve_both_roles():
    """selected_model falls back to the default whenever a vision pick is unusable,
    so the default being text-only would break photos with no way out."""
    assert ai._BY_ID[ai.DEFAULT_MODEL_ID]["vision"] is True


def test_every_provider_can_be_selected_from():
    """Each provider a household might hold a key for needs at least one row,
    or that key buys them nothing."""
    covered = {m["provider"] for m in ai.AI_MODELS}
    assert covered == set(ai.PROVIDERS), set(ai.PROVIDERS) - covered


@pytest.mark.parametrize("provider", ai.PROVIDERS)
@pytest.mark.parametrize("rec_key", ("recVision", "recText"))
def test_every_provider_recommends_something(provider, rec_key):
    """A household holding just one key should still be told where to start, in
    both pickers. Several per provider is fine — they answer different questions
    (cheapest that works, best all-round) — but none leaves that key unguided."""
    assert [m for m in ai.AI_MODELS if m["provider"] == provider and m.get(rec_key)]
