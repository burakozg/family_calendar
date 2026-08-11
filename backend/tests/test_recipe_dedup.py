"""F8: keep original language (prompt guard), find_similar duplicate detection,
the pending-import queue, and merge/create/discard resolution."""
import base64
import io
import zipfile

import pytest

import ai
import recipes
import storage
from storage import read_recipe_file, read_recipe_index


@pytest.fixture(autouse=True)
def clean(client):
    for d in (storage.RECIPES_DIR, storage.RECIPE_PENDING_DIR):
        for p in d.glob("*.json"):
            if p.name != storage.F_RECIPE_IDX.name:
                p.unlink()
    storage.write_recipe_index([])
    yield


def _mk(client, rid, name, source, ings=None, **extra):
    client.post("/recipes", json={"id": rid, "name": name, "source": source,
                                  "ingredients": [{"item": i} for i in (ings or [])], **extra})


# ── F8a: prompt language guard ────────────────────────────────────────────────
def test_prompts_keep_original_language():
    for p in (recipes.EXTRACT_SYSTEM, recipes.EXTRACT_TEXT_SYSTEM):
        assert "in ENGLISH: translate" not in p          # no blanket translation
        assert "ORIGINAL language" in p
        assert "TAXONOMY fields in English" in p


def test_translate_toggle_swaps_the_language_clause():
    for build in (recipes.extract_system, recipes.extract_text_system):
        keep = build(False)
        assert "ORIGINAL language" in keep and "TRANSLATE the content fields" not in keep
        tr = build(True)
        assert "TRANSLATE the content fields into ENGLISH" in tr
        assert "KEEP the content fields in the ORIGINAL language" not in tr
        # only the language clause changes — the rest of the prompt is identical
        assert keep.replace(recipes._KEEP_LANG_CLAUSE, "X") == tr.replace(recipes._TRANSLATE_LANG_CLAUSE, "X")


# ── F8b: find_similar ─────────────────────────────────────────────────────────
def test_find_similar_source_identity_across_languages(client):
    _mk(client, "afr", "Air Fryer Ratatouille", {"type": "notion", "value": "Ratatouille"})
    m = recipes.find_similar("Fırında Sebze Türlüsü", {"type": "notion", "value": "Ratatouille"})
    assert m and m[0]["id"] == "afr" and m[0]["reason"] == "same source" and m[0]["score"] == 1.0


def test_find_similar_url_identity(client):
    _mk(client, "x", "Some Dish", {"type": "url", "value": "https://site.example/x"})
    m = recipes.find_similar("Totally Different Name", {"type": "url", "value": "https://site.example/x"})
    assert m and m[0]["id"] == "x" and m[0]["reason"] == "same source"


def test_find_similar_folded_name(client):
    _mk(client, "kofte", "Köfte", {"type": "memory", "value": ""})
    m = recipes.find_similar("kofte", {"type": "photo", "value": ""})
    assert m and m[0]["id"] == "kofte" and m[0]["reason"] == "similar name"


def test_find_similar_ingredient_overlap(client):
    _mk(client, "lentil", "Lentil Soup", {"type": "memory", "value": ""},
        ings=["Red lentils", "Onion", "Carrot", "Cumin"])
    # name ratio 0.733 (< 0.75, so tier 2 misses) + high ingredient Jaccard → tier 3
    m = recipes.find_similar("Red Lentil Soup Pot", {"type": "photo", "value": ""},
                             ["red lentils", "onion", "carrot", "cumin"])
    assert m and m[0]["id"] == "lentil" and m[0]["reason"] == "shared ingredients"


def test_find_similar_below_threshold_empty(client):
    _mk(client, "pie", "Apple Pie", {"type": "memory", "value": ""}, ings=["apple", "flour", "butter"])
    assert recipes.find_similar("Beef Tacos", {"type": "photo", "value": ""}, ["beef", "tortilla"]) == []


def test_similar_route(client):
    _mk(client, "afr", "Air Fryer Ratatouille", {"type": "notion", "value": "Ratatouille"})
    r = client.get("/recipes/similar", params={"name": "x", "source_type": "notion",
                                               "source_value": "Ratatouille"})
    assert r.json()["matches"][0]["id"] == "afr"


# ── F8c: pending queue + resolve ──────────────────────────────────────────────
def _queue_one(client, match_id, name="New Draft", **fields):
    recipe = {"name": name, "ingredients": [{"item": "onion"}], "steps": [{"text": "cook"}], **fields}
    r = client.post("/recipes/pending", json={"recipe": recipe, "matchId": match_id,
                                              "matchName": "Existing", "matchScore": 1.0, "who": "owner"})
    return r.json()["pendingId"]


def test_pending_create_and_list(client):
    _mk(client, "existing", "Existing", {"type": "notion", "value": "P"})
    pid = _queue_one(client, "existing")
    rows = client.get("/recipes/pending").json()
    assert len(rows) == 1 and rows[0]["pendingId"] == pid and rows[0]["matchId"] == "existing"
    assert client.get(f"/recipes/pending/{pid}").json()["name"] == "New Draft"


def test_resolve_merge_overwrites_only_listed_and_preserves_rating(client):
    _mk(client, "existing", "Old Name", {"type": "notion", "value": "P"},
        ings=["old ing"], rating=5, cuisine="Italian", course="main",
        log={"entered_by": "owner", "entered_at": "2020-01-01"})
    pid = _queue_one(client, "existing", name="Köfte", cuisine="Turkish",
                     ingredients=[{"item": "kıyma"}], steps=[{"text": "yoğur"}])
    r = client.post(f"/recipes/pending/{pid}/resolve",
                    json={"action": "merge", "fields": ["name", "ingredients", "steps"]})
    assert r.json()["ok"] and r.json()["id"] == "existing"
    m = read_recipe_file("existing")
    assert m["name"] == "Köfte"                           # taken from draft
    assert m["ingredients"][0]["item"] == "kıyma"          # original-language content
    assert m["cuisine"] == "Italian"                       # NOT in fields → preserved
    assert m["rating"] == 5 and m["log"]["entered_at"] == "2020-01-01"   # always preserved
    assert not storage.read_pending_recipe(pid)            # pending file gone
    assert any(e["id"] == "existing" and e["name"] == "Köfte" for e in read_recipe_index())


def test_resolve_merge_unions_photos(client):
    _mk(client, "existing", "Dish", {"type": "notion", "value": "P"})
    ex = read_recipe_file("existing"); ex["photos"] = [{"id": "a"}]
    storage.write_recipe_file("existing", ex)
    recipe = {"name": "Dish", "photos": [{"id": "b"}], "ingredients": [{"item": "x"}]}
    pid = client.post("/recipes/pending", json={"recipe": recipe, "matchId": "existing"}).json()["pendingId"]
    client.post(f"/recipes/pending/{pid}/resolve", json={"action": "merge", "fields": ["name"]})
    ids = {p["id"] for p in read_recipe_file("existing")["photos"]}
    assert ids == {"a", "b"}


def test_resolve_create_and_discard(client):
    _mk(client, "existing", "Existing", {"type": "notion", "value": "P"})
    pid = _queue_one(client, "existing", name="Brand New")
    r = client.post(f"/recipes/pending/{pid}/resolve", json={"action": "create"})
    assert r.json()["action"] == "create"
    assert read_recipe_file(r.json()["id"])["name"] == "Brand New"
    assert not storage.read_pending_recipe(pid)

    pid2 = _queue_one(client, "existing", name="To Discard")
    client.post(f"/recipes/pending/{pid2}/resolve", json={"action": "discard"})
    assert not storage.read_pending_recipe(pid2)
    assert not any(e["name"] == "To Discard" for e in read_recipe_index())


def _notion_zip(title):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"Export/{title} {'a'*32}.md",
                   f"# {title}\n\nA proper recipe note with enough prose that it is not treated "
                   "as an index page. Mix, season, and bake until done and golden.")
    return base64.b64encode(buf.getvalue()).decode()


def test_notion_import_queues_a_match_not_a_duplicate(client, monkeypatch):
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")

    async def fake_complete(system, messages, max_tokens, *, action, timeout=45):
        return ('{"name":{"value":"Ratatouille","source":"extracted"},'
                '"ingredients":{"value":[{"item":{"value":"aubergine"}}],"source":"extracted"},'
                '"steps":{"value":[{"text":{"value":"bake"}}],"source":"extracted"}}')
    monkeypatch.setattr(ai, "complete", fake_complete)

    _mk(client, "old", "Old Translated Name", {"type": "notion", "value": "Ratatouille"})
    zb = _notion_zip("Ratatouille")

    r = client.post("/recipes/import-notion", json={"zip": zb, "who": "owner"}).json()
    assert len(r["pending"]) == 1 and len(r["imported"]) == 0        # queued, not duplicated
    assert storage.list_pending_recipes()[0]["matchId"] == "old"
    assert not any(e["id"] != "old" for e in read_recipe_index())    # no new recipe created

    r2 = client.post("/recipes/import-notion",
                     json={"zip": zb, "who": "owner", "onDuplicate": "create"}).json()
    assert len(r2["imported"]) == 1 and len(r2["pending"]) == 0       # bypass → creates
