"""Provenance unwrapping is not one level deep. The AI extractor wraps a draft's
list *elements* and a nested dict's *sub-fields* in {value, source} as often as it
wraps the field itself, and unwrapping only the outer layer leaves dicts where
strings are expected. Both known symptoms are covered here: `", ".join(tags)`
raising TypeError (one bad recipe 500'd every meal-planner call) and
`source.get("type").strip()` raising AttributeError (photo scans 500'd)."""
import meals
import recipes


def _draft_lists(**kw) -> dict:
    return {"name": "Izgara Somon", **kw}


def test_draft_unwraps_per_element_string_lists():
    r = recipes._recipe_from_draft(_draft_lists(
        tags=[{"value": "grilled", "source": "extracted"},
              {"value": "salmon", "source": "extracted"}],
        meal_type=[{"value": "dinner", "source": "suggested"}],
        dietary=[{"value": "pescatarian", "source": "suggested"}],
        equipment=[{"value": "grill", "source": "extracted"}],
    ), {"type": "text", "value": ""})
    assert r["tags"] == ["grilled", "salmon"]
    assert r["meal_type"] == ["dinner"]
    assert r["dietary"] == ["pescatarian"]
    assert r["equipment"] == ["grill"]


def test_draft_keeps_plain_lists_and_drops_blanks():
    r = recipes._recipe_from_draft(_draft_lists(
        tags={"value": ["quick", "", {"value": "simple", "source": "extracted"}],
              "source": "extracted"},          # wrapped field *and* a wrapped element
        meal_type=None,
    ), {"type": "text", "value": ""})
    assert r["tags"] == ["quick", "simple"]
    assert r["meal_type"] == []


def test_draft_source_unwraps_both_levels():
    """The extractor wraps `source` AND each of its sub-fields."""
    draft = {"source": {"value": {"type":  {"value": "url", "source": "extracted"},
                                  "value": {"value": "https://ex.test/r", "source": "extracted"},
                                  "author": {"value": "", "source": "empty"}},
                        "source": "extracted"}}
    assert recipes._draft_source(draft) == {"type": "url", "value": "https://ex.test/r"}


def test_draft_source_tolerates_a_missing_or_odd_source():
    assert recipes._draft_source({}) == {}
    assert recipes._draft_source({"source": "not a dict"}) == {}
    assert recipes._draft_source({"source": {"type": "photo"}}) == {"type": "photo", "value": ""}


def test_find_similar_survives_a_half_unwrapped_source(client):
    """Photo scans 500'd here with `'dict' object has no attribute 'strip'`."""
    assert recipes.find_similar("Ugnsstek kyckling",
                                {"type": {"value": "unknown"}, "value": {"value": ""}}) == []


def test_planner_line_survives_a_malformed_tag_list():
    """Belt and braces: an already-saved bad recipe must not take the week down."""
    line = meals._recipe_option_line({
        "id": "izgara-somon", "name": "Izgara Somon", "cuisine": "Turkish",
        "tags": [{"value": "grilled", "source": "extracted"}, "salmon"],
    })
    assert line.startswith("- [izgara-somon] Izgara Somon")
    assert "salmon" in line
