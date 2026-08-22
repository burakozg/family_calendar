"""F8d: a meal-kit sheet (Hello Fresh and friends) puts a main and its side on one
page — one ingredient table, one interleaved method. Imported whole, the side is
unfindable on its own and the main's ingredients are wrong for any other pairing.

The split is proposed, never applied: the import queues it under Pending imports and
a human accepts, keeps it as one, or discards."""
import asyncio
import json

import ai
import pytest
import recipes
import storage


@pytest.fixture(autouse=True)
def clean_recipes(client):
    for p in storage.RECIPES_DIR.glob("*.json"):
        if p.name != storage.F_RECIPE_IDX.name:
            p.unlink()
    for p in storage.RECIPE_PENDING_DIR.glob("*.json"):
        p.unlink()
    storage.write_recipe_index([])
    yield


def _sheet(**over):
    """A combined recipe in saved shape, as the extractor leaves it."""
    r = {"name": "Ugnsstek kyckling med potatissallad", "description": "", "cuisine": "Swedish",
         "course": "main", "tags": ["kyckling"], "meal_type": [], "dietary": [],
         "servings": 4, "difficulty": 2, "prep_time_min": 15, "cook_time_min": 35,
         "ingredients": [{"amount": str(n), "unit": "g", "item": item, "notes": "", "category": ""}
                         for n, item in ((300, "kycklingbröstfilé"), (13, "ströbröd"), (1, "ägg"),
                                         (400, "potatis"), (250, "broccoli"), (150, "kålmix"),
                                         (40, "aioli"))],
         "equipment": ["ugn"],
         "steps": [{"text": t, "duration_min": 0} for t in
                   ("Rosta potatis i ugnen.", "Panera kyckling.", "Stek kyckling.",
                    "Ugnsstek kyckling.", "Blanda sallad.", "Servera.")],
         "notes": "", "source": {"type": "photo", "value": ""},
         "log": {"entered_by": "burak", "entered_at": "2026-08-18"}}
    r.update(over)
    return r


SPLIT_REPLY = json.dumps({"split": True,
    "main": {"name": "Panerad ugnsstekt kyckling", "course": "main",
             "ingredients": [{"amount": "300", "unit": "g", "item": "kycklingbröstfilé"},
                             {"amount": "13", "unit": "g", "item": "ströbröd"},
                             {"amount": "1", "unit": "", "item": "ägg"}],
             "steps": [{"text": "Panera kyckling."}, {"text": "Stek kyckling."},
                       {"text": "Ugnsstek i 20 minuter."}, {"text": "Servera med salladen."}],
             "tags": ["kyckling"], "equipment": ["ugn"], "prep_time_min": 10, "cook_time_min": 25},
    "side": {"name": "Potatissallad med broccoli", "course": "side",
             "ingredients": [{"amount": "400", "unit": "g", "item": "potatis"},
                             {"amount": "250", "unit": "g", "item": "broccoli"},
                             {"amount": "150", "unit": "g", "item": "kålmix"},
                             {"amount": "40", "unit": "g", "item": "aioli"}],
             "steps": [{"text": "Rosta potatis och broccoli i ugnen i 25 minuter."},
                       {"text": "Blanda med kålmix och aioli."}],
             "tags": ["sallad"], "equipment": [], "prep_time_min": 10, "cook_time_min": 25}})


@pytest.fixture()
def ai_reply(monkeypatch):
    """Stand in for the split call; `box['reply']` is what the model 'returns'."""
    box = {"reply": SPLIT_REPLY, "calls": []}

    async def fake(system, messages, max_tokens, *, action, timeout=45):
        box["calls"].append({"action": action, "max_tokens": max_tokens,
                             "user": messages[0]["content"]})
        if isinstance(box["reply"], Exception):
            raise box["reply"]
        return box["reply"]

    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(ai, "complete", fake)
    return box


# ── proposing ────────────────────────────────────────────────────────────────
# Driven with asyncio.run rather than @pytest.mark.anyio: the anyio plugin runs the
# coroutine on its own loop while the TestClient fixture holds another, and the two
# together wedged a later test in this file that takes the write lock twice.
def _propose(recipe):
    return asyncio.run(recipes._propose_split(recipe))


def test_proposes_two_cookable_dishes(ai_reply):
    main, side = _propose(_sheet())

    assert (main["name"], main["course"]) == ("Panerad ugnsstekt kyckling", "main")
    assert (side["name"], side["course"]) == ("Potatissallad med broccoli", "side")
    # Ingredients are divided, not copied into both.
    assert [i["item"] for i in main["ingredients"]] == ["kycklingbröstfilé", "ströbröd", "ägg"]
    assert "potatis" not in [i["item"] for i in main["ingredients"]]
    # Each half inherits what identifies the source.
    assert main["cuisine"] == side["cuisine"] == "Swedish"
    assert main["source"] == side["source"] == {"type": "photo", "value": ""}
    # Ids and photos belong to the save step, not the proposal.
    assert "id" not in main and "photos" not in main


def test_sends_only_the_recipe_json_not_the_image(ai_reply):
    _propose(_sheet())

    call = ai_reply["calls"][0]
    assert call["action"] == "recipe.split"          # its own label in the activity log
    assert call["max_tokens"] == recipes.EXTRACT_MAX_TOKENS
    sent = json.loads(call["user"])
    assert sent["name"] == "Ugnsstek kyckling med potatissallad"
    assert "image" not in sent and "photos" not in sent


@pytest.mark.parametrize("reply, why", [
    (json.dumps({"split": False}), "model says one dish"),
    (json.dumps({"split": True, "main": {"name": "A"}}), "only one half returned"),
    (json.dumps({"split": True, "main": {"name": "Same", "ingredients": [{"item": "x"}]},
                 "side": {"name": "same", "ingredients": [{"item": "y"}]}}), "same dish twice"),
    (json.dumps({"split": True, "main": {"name": "A", "ingredients": [{"item": "x"}]},
                 "side": {"name": "B", "ingredients": []}}), "a half with no ingredients"),
    (json.dumps({"split": True,
                 "main": {"name": "A", "ingredients": [{"item": "x"}], "steps": []},
                 "side": {"name": "B", "ingredients": [{"item": "y"}], "steps": []}}),
     "both halves without a method — what the 30B model actually returns"),
    ("not json at all", "unparseable reply"),
])
def test_declines_to_split_when_unsure(ai_reply, reply, why):
    ai_reply["reply"] = reply
    assert _propose(_sheet()) is None, why


def test_an_ai_failure_never_breaks_the_import(ai_reply):
    """The import has already succeeded by this point, and the relay retries a failed
    command from the top — an optional extra must not trigger a re-extraction."""
    ai_reply["reply"] = RuntimeError("provider down")
    assert _propose(_sheet()) is None


def test_skips_the_call_for_an_obviously_single_dish(ai_reply):
    small = _sheet(ingredients=[{"item": "egg"}], steps=[{"text": "boil"}])
    assert _propose(small) is None
    assert ai_reply["calls"] == []                   # no spend on the implausible case


# ── resolving ────────────────────────────────────────────────────────────────
def _queue(client, **over):
    """Put a split proposal on disk the way the photo import would."""
    recipe = _sheet(**over)
    recipe["id"] = "pend123"
    main = {**_sheet(), "name": "Panerad kyckling", "course": "main",
            "ingredients": [{"item": "kyckling"}], "steps": [{"text": "Stek."}]}
    side = {**_sheet(), "name": "Potatissallad", "course": "side",
            "ingredients": [{"item": "potatis"}], "steps": [{"text": "Rosta."}]}
    recipes._queue_split("pend123", recipe, main, side, "burak")
    return "pend123"


def test_pending_list_marks_a_split_and_names_both_dishes(client):
    _queue(client)
    row = client.get("/recipes/pending").json()[0]

    assert row["kind"] == "split"
    assert row["proposedNames"] == ["Panerad kyckling", "Potatissallad"]


def test_a_row_written_before_kind_existed_still_reads_as_a_duplicate(client):
    """Items already on disk have no `kind`; absent must not become 'split'."""
    storage.write_pending_recipe("old1", {**_sheet(), "pendingId": "old1", "matchId": "x",
                                          "matchName": "Old", "created": "2026-01-01"})
    row = next(r for r in client.get("/recipes/pending").json() if r["pendingId"] == "old1")

    assert row["kind"] == "duplicate"
    assert client.post("/recipes/pending/old1/resolve",
                       json={"action": "split_both"}).status_code == 400


def test_save_both_creates_two_cross_referenced_recipes(client):
    pid = _queue(client)
    r = client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"})

    assert r.status_code == 200 and len(r.json()["ids"]) == 2
    saved = {x["name"]: storage.read_recipe_file(x["id"])
             for x in storage.read_recipe_index()}
    assert set(saved) == {"Panerad kyckling", "Potatissallad"}
    assert saved["Panerad kyckling"]["course"] == "main"
    assert saved["Potatissallad"]["course"] == "side"
    # The pairing survives as prose, since nothing follows a recipe → recipe link.
    assert "Serve with: Potatissallad" in saved["Panerad kyckling"]["notes"]
    assert "Serve with: Panerad kyckling" in saved["Potatissallad"]["notes"]
    # Both stay in the amber review list until a human saves them.
    assert all(v["needs_review"] for v in saved.values())
    assert storage.read_pending_recipe(pid) is None


def test_save_both_shares_the_source_photo_with_each_half(client):
    pid = _queue(client, photos=[{"id": "p1", "path": "photos/pend123/p1.jpg", "kind": "source"}])
    client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"})

    for e in storage.read_recipe_index():
        assert storage.read_recipe_file(e["id"])["photos"][0]["id"] == "p1"


def test_a_half_that_duplicates_an_existing_recipe_goes_to_the_merge_queue(client):
    client.post("/recipes", json={"id": "potatissallad", "name": "Potatissallad"})
    pid = _queue(client)

    body = client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"}).json()

    assert len(body["ids"]) == 1 and len(body["pendingIds"]) == 1
    names = {e["name"] for e in storage.read_recipe_index()}
    assert names == {"Potatissallad", "Panerad kyckling"}      # no second copy
    queued = storage.read_pending_recipe(body["pendingIds"][0])
    assert queued["kind"] == "duplicate" and queued["matchId"] == "potatissallad"


def test_keep_as_one_saves_the_combined_recipe(client):
    pid = _queue(client)
    r = client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_none"})

    assert r.status_code == 200
    idx = storage.read_recipe_index()
    assert [e["name"] for e in idx] == ["Ugnsstek kyckling med potatissallad"]
    assert storage.read_recipe_file(idx[0]["id"])["needs_review"] is True
    assert storage.read_pending_recipe(pid) is None


def test_discard_still_works_on_a_split(client):
    pid = _queue(client)
    assert client.post(f"/recipes/pending/{pid}/resolve", json={"action": "discard"}).status_code == 200
    assert storage.read_pending_recipe(pid) is None
    assert storage.read_recipe_index() == []


# ── attribution ──────────────────────────────────────────────────────────────
def test_both_halves_are_attributed_to_the_meal_kit(client):
    """Nobody in the house wrote a Hello Fresh page — a person only photographed it.
    The scanner's name ("burak") must not end up as the author of either dish."""
    pid = _queue(client)
    client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"})

    for e in storage.read_recipe_index():
        assert storage.read_recipe_file(e["id"])["log"]["entered_by"] == recipes.MEAL_KIT_ENTERER


def test_attribution_survives_dict_copied_log(client):
    """_dish_from_split copies the original recipe wholesale, so each half arrives
    with the scanner's `log` already set. A setdefault here is a silent no-op."""
    pid = _queue(client)
    client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"})
    saved = [storage.read_recipe_file(e["id"]) for e in storage.read_recipe_index()]

    assert saved and all(r["log"]["entered_by"] != "burak" for r in saved)
    # The date the sheet was scanned is kept; only the author changes.
    assert all(r["log"]["entered_at"] == "2026-08-18" for r in saved)


def test_a_duplicate_half_is_queued_as_meal_kit_too(client):
    client.post("/recipes", json={"id": "potatissallad", "name": "Potatissallad"})
    pid = _queue(client)

    body = client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_both"}).json()

    assert storage.read_pending_recipe(body["pendingIds"][0])["who"] == recipes.MEAL_KIT_ENTERER


def test_keeping_it_as_one_leaves_the_scanner_as_author(client):
    """Only a split reattributes. Declining it means the human called it one dish,
    and the ordinary photo-scan path is untouched by any of this."""
    pid = _queue(client)
    client.post(f"/recipes/pending/{pid}/resolve", json={"action": "split_none"})

    idx = storage.read_recipe_index()
    assert storage.read_recipe_file(idx[0]["id"])["log"]["entered_by"] == "burak"
