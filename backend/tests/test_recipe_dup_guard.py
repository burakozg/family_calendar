"""Adding a recipe that already exists.

Two halves: the editor must be able to ASK about look-alikes however the recipe got
there (/recipes/similar), and a direct save must never destroy an existing recipe
when it can't. Before this, POST /recipes slugified the name straight to an id and
overwrote the previous holder with no warning and no log line.
"""
import storage


def _fresh():
    for p in storage.RECIPES_DIR.glob("*.json"):
        if p.name != storage.F_RECIPE_IDX.name:
            p.unlink()
    storage.write_recipe_index([])


# ── the save must not destroy ────────────────────────────────────────────────
def test_same_name_creates_a_second_recipe_instead_of_overwriting(client):
    _fresh()
    client.post("/recipes", json={"name": "Meatballs", "steps": [{"text": "Original method"}]})
    client.post("/recipes", json={"name": "Meatballs", "steps": [{"text": "Different method"}]})

    idx = storage.read_recipe_index()
    assert len(idx) == 2, "the second save replaced the first instead of adding"
    assert {e["id"] for e in idx} == {"meatballs", "meatballs-2"}
    kept = storage.read_recipe_file("meatballs")
    assert [s["text"] for s in kept["steps"]] == ["Original method"]   # untouched


def test_an_explicit_id_still_updates_in_place(client):
    """Passing an id is how the app re-saves a known recipe; that must keep working."""
    _fresh()
    client.post("/recipes", json={"id": "kofte", "name": "Köfte", "steps": [{"text": "v1"}]})
    client.post("/recipes", json={"id": "kofte", "name": "Köfte", "steps": [{"text": "v2"}]})

    assert len(storage.read_recipe_index()) == 1
    assert [s["text"] for s in storage.read_recipe_file("kofte")["steps"]] == ["v2"]


# ── the editor must be able to ask ───────────────────────────────────────────
def test_similar_matches_on_name(client):
    _fresh()
    client.post("/recipes", json={"name": "Meatballs"})
    m = client.get("/recipes/similar", params={"name": "Meat balls"}).json()["matches"]

    assert [x["name"] for x in m] == ["Meatballs"]
    assert m[0]["reason"] == "similar name"


def test_similar_uses_ingredients_to_catch_a_renamed_copy(client):
    """The gate that makes checking on every entry worth it: a name ratio alone
    misses "Swedish Meatballs" against "Meatballs"."""
    _fresh()
    client.post("/recipes", json={"name": "Meatballs", "ingredients": [
        {"item": "beef"}, {"item": "onion"}, {"item": "breadcrumbs"}, {"item": "egg"}]})

    bare = client.get("/recipes/similar", params={"name": "Swedish Meatballs"}).json()["matches"]
    assert bare == []                                     # name alone: no match

    withi = client.get("/recipes/similar", params=[
        ("name", "Swedish Meatballs"), ("ingredient", "beef"), ("ingredient", "onion"),
        ("ingredient", "breadcrumbs"), ("ingredient", "egg")]).json()["matches"]
    assert [x["name"] for x in withi] == ["Meatballs"]
    assert withi[0]["reason"] == "shared ingredients"


def test_similar_excludes_the_recipe_being_edited(client):
    """Opening a saved recipe would otherwise always flag it as a duplicate of itself."""
    _fresh()
    client.post("/recipes", json={"id": "meatballs", "name": "Meatballs"})

    assert client.get("/recipes/similar", params={"name": "Meatballs"}).json()["matches"]
    assert client.get("/recipes/similar",
                      params={"name": "Meatballs", "exclude": "meatballs"}).json()["matches"] == []


def test_similar_matches_on_source_identity_despite_a_different_name(client):
    _fresh()
    client.post("/recipes", json={"name": "Totally Different Name",
                                  "source": {"type": "url", "value": "https://x.test/r/1"}})
    m = client.get("/recipes/similar", params={
        "name": "Nothing Alike", "source_type": "url",
        "source_value": "https://x.test/r/1"}).json()["matches"]

    assert m and m[0]["reason"] == "same source"
