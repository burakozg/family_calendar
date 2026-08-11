"""F3: recipe index extension (course/dietary/servings/total_time_min/
ingredients/source_value), the rebuild helper, the schema-probe, and the
reindex route."""
import pytest

import storage
from storage import _recipe_index_entry, rebuild_recipe_index, recipe_index_needs_rebuild


@pytest.fixture(autouse=True)
def clean_recipes(client):
    for p in storage.RECIPES_DIR.glob("*.json"):
        if p.name != storage.F_RECIPE_IDX.name:
            p.unlink()
    storage.write_recipe_index([])
    yield


def test_index_entry_has_new_fields():
    e = _recipe_index_entry({
        "id": "x", "name": "Test", "cuisine": "Italian", "course": "main",
        "dietary": ["vegan"], "servings": 4,
        "prep_time_min": 10, "cook_time_min": 20, "inactive_time_min": 5,
        "ingredients": [{"item": "Onion"}, {"item": "  "}, {"item": "Garlic"}],
        "equipment": ["Air fryer", "  ", "Whisk"],
        "source": {"type": "notion", "value": "Pasta Page"},
    })
    assert e["total_time_min"] == 35                       # prep + cook + inactive
    assert e["dietary"] == ["vegan"] and e["servings"] == 4
    assert e["ingredients"] == ["onion", "garlic"]         # lowercased, blanks dropped
    assert e["equipment"] == ["air fryer", "whisk"]        # lowercased, blanks dropped (search)
    assert e["source_value"] == "Pasta Page"


def test_needs_rebuild_probe():
    storage.write_recipe_index([{"id": "a", "name": "A"}])          # legacy row
    assert recipe_index_needs_rebuild() is True
    storage.write_recipe_index([_recipe_index_entry({"id": "a", "name": "A"})])
    assert recipe_index_needs_rebuild() is False
    storage.write_recipe_index([])                                  # empty → lazy, no rebuild
    assert recipe_index_needs_rebuild() is False


def test_rebuild_reads_all_files_sorted(client):
    client.post("/recipes", json={"id": "zeta", "name": "Zeta", "servings": 2})
    client.post("/recipes", json={"id": "alpha", "name": "Alpha", "servings": 6})
    storage.write_recipe_index([{"id": "stale", "name": "stale"}])   # corrupt the index
    n = rebuild_recipe_index()
    idx = storage.read_recipe_index()
    assert n == 2 and [e["id"] for e in idx] == ["alpha", "zeta"]    # name-sorted, rebuilt from files
    assert all("total_time_min" in e for e in idx)


def test_reindex_route(client):
    client.post("/recipes", json={"id": "r", "name": "R"})
    storage.write_recipe_index([{"id": "r", "name": "R"}])           # legacy shape
    assert recipe_index_needs_rebuild() is True
    r = client.post("/recipes/reindex")
    assert r.status_code == 200 and r.json()["count"] == 1
    assert recipe_index_needs_rebuild() is False
