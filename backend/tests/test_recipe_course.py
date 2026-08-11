"""F4: recipe `course` — enum coercion on create/update, the classify-courses
backfill (mocked AI + keyword fallback), and prompt coverage."""
import json

import pytest

import ai
import recipes
import storage
from storage import RECIPE_COURSES, read_recipe_file, read_recipe_index


@pytest.fixture(autouse=True)
def clean_recipes(client):
    """The test DATA_DIR is shared across the session; start each test with an
    empty recipe library so default (no-ids) classify targeting is deterministic."""
    for p in storage.RECIPES_DIR.glob("*.json"):
        if p.name != storage.F_RECIPE_IDX.name:
            p.unlink()
    storage.write_recipe_index([])
    yield


def _find(index, rid):
    return next((e for e in index if e["id"] == rid), None)


def test_create_coerces_invalid_course(client):
    client.post("/recipes", json={"id": "r-bad", "name": "Bad course", "course": "entrée"})
    client.post("/recipes", json={"id": "r-good", "name": "Good course", "course": "SOUP"})
    assert read_recipe_file("r-bad")["course"] == ""        # invalid → ""
    assert read_recipe_file("r-good")["course"] == "soup"    # normalized (lowercased)
    idx = read_recipe_index()
    assert _find(idx, "r-bad")["course"] == ""
    assert _find(idx, "r-good")["course"] == "soup"


def test_update_coerces_invalid_course(client):
    client.post("/recipes", json={"id": "r-up", "name": "Upd", "course": "main"})
    client.put("/recipes/r-up", json={"id": "r-up", "name": "Upd", "course": "not-a-course"})
    assert read_recipe_file("r-up")["course"] == ""


def test_keyword_course_fallback():
    assert recipes._keyword_course("Kırmızı Mercimek Çorbası") == "soup"
    assert recipes._keyword_course("Sommarsallad med tomat") == "salad"
    assert recipes._keyword_course("Chocolate Cake") == "dessert"
    assert recipes._keyword_course("Sourdough bread") == "baking"
    assert recipes._keyword_course("Mango smoothie") == "drink"
    assert recipes._keyword_course("Spaghetti Bolognese") == "main"   # default


def test_classify_courses_uses_ai_then_falls_back(client, monkeypatch):
    client.post("/recipes", json={"id": "c-1", "name": "Tomato Soup"})
    client.post("/recipes", json={"id": "c-2", "name": "Roast Chicken"})
    client.post("/recipes", json={"id": "c-3", "name": "Mystery Kurabiye"})

    async def fake(system, messages, max_tokens, *, action, timeout=45):
        # c-1 valid, c-2 invalid (→ keyword fallback), c-3 omitted (→ keyword fallback)
        return json.dumps({"c-1": "soup", "c-2": "entrée"})

    monkeypatch.setattr(ai, "complete_or_none", fake)
    r = client.post("/recipes/classify-courses", json={})
    assert r.status_code == 200 and set(r.json()["classified"]) == {"c-1", "c-2", "c-3"}
    assert read_recipe_file("c-1")["course"] == "soup"        # from AI
    assert read_recipe_file("c-2")["course"] == "main"        # invalid AI answer → keyword ("Roast Chicken")
    assert read_recipe_file("c-3")["course"] == "dessert"     # AI silent → keyword ("Kurabiye")


def test_classify_courses_only_targets_missing(client, monkeypatch):
    client.post("/recipes", json={"id": "keep", "name": "Already", "course": "salad"})
    client.post("/recipes", json={"id": "fill", "name": "Needs one"})

    seen = {}

    async def fake(system, messages, max_tokens, *, action, timeout=45):
        seen["items"] = json.loads(messages[0]["content"])
        return json.dumps({"fill": "main"})

    monkeypatch.setattr(ai, "complete_or_none", fake)
    client.post("/recipes/classify-courses", json={})
    assert [i["id"] for i in seen["items"]] == ["fill"]        # 'keep' already has a course
    assert read_recipe_file("keep")["course"] == "salad"       # untouched


def test_no_key_classify_falls_back_to_keywords(client):
    # conftest leaves ANTHROPIC_API_KEY empty → complete_or_none returns None.
    client.post("/recipes", json={"id": "nk", "name": "Lentil Soup"})
    r = client.post("/recipes/classify-courses", json={})
    assert r.json()["count"] >= 1
    assert read_recipe_file("nk")["course"] == "soup"


def test_prompts_mention_course_enum():
    for prompt in (recipes.EXTRACT_SYSTEM, recipes.EXTRACT_TEXT_SYSTEM, recipes.RECIPE_GEN_SYSTEM):
        assert "course" in prompt
        for c in RECIPE_COURSES:
            assert c in prompt
