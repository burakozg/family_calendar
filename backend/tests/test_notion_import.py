"""Notion import: companion-folder image resolution + screenshot pages routed to
vision extraction (with the screenshot kept as the recipe photo)."""
import base64
import io
import json
import zipfile

import ai
import recipes
import storage

# 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def _zip_b64(files: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return base64.b64encode(buf.getvalue()).decode()


def test_extraction_prompts_recommend_missing_fields():
    # Both extractors must instruct the model to estimate the numeric fields
    # (times, servings), not just the categorical ones.
    for prompt in (recipes.EXTRACT_SYSTEM, recipes.EXTRACT_TEXT_SYSTEM):
        assert '"suggested"' in prompt
        # These must be in the *infer* clause so imports fill them even when the
        # source doesn't spell them out (equipment was previously only extracted).
        infer = prompt.split("infer", 1)[1].split("Use \"empty\"", 1)[0]
        for field in ("servings", "prep_time_min", "cook_time_min", "cuisine", "tags", "equipment"):
            assert field in infer, field


def test_page_images_resolves_folder_and_skips_non_local():
    md = ("# Pasta\n\n![shot](Pasta%20abc123/shot.png)\n"
          "![ext](https://example.com/x.png)\n![doc](Pasta%20abc123/notes.txt)\n")
    files = {
        "Export/Recipes/Pasta abc123.md": md.encode(),
        "Export/Recipes/Pasta abc123/shot.png": PNG,
        "Export/Recipes/Pasta abc123/notes.txt": b"not an image",
    }
    lookup = {p.lower(): (p, d) for p, d in files.items()}
    got = recipes._page_images(lookup, "Export/Recipes/Pasta abc123.md", md, 4)
    assert len(got) == 1                                   # external URL + .txt skipped
    data, media, ext = got[0]
    assert data == PNG and media == "image/png" and ext == "png"


def test_import_real_notion_tree_screenshot_recipe(client, monkeypatch):
    """Mirror a real 'Markdown & CSV' export: nested category folders, an index
    page per level, and a recipe page that is just an embedded screenshot living
    in a companion folder. The recipe must import (via vision); index pages must
    be skipped; the 'No recipe pages found' error must not fire."""
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")

    async def fake_complete(system, messages, max_tokens, *, action, timeout=45):
        content = messages[0]["content"]
        img = isinstance(content, list) and any(b.get("type") == "image" for b in content)
        return json.dumps({"name": "Airfrier da somon" if img else "Text",
                           "ingredients": [{"item": "salmon", "amount": "2", "unit": ""}],
                           "steps": [{"text": "airfry"}]})
    monkeypatch.setattr(ai, "complete", fake_complete)

    root = "Sam's Recipee Book"
    cat  = f"{root}/Ana yemekler"
    # Index pages: links to child .md pages, little prose → filtered as index.
    idx  = "# X\n\n" + "".join(f"[Child {i}](Child%20{i}.md)\n" for i in range(4))
    # Screenshot recipe page: title + a plain image link (no leading `!`).
    shot = "# Airfrier da somon\n\n[IMG_5485.png](Airfrier%20da%20somon/IMG_5485.png)\n"
    zip_b64 = _zip_b64({
        f"{root}/Sam's Recipee Book 2 f44fdb0cddaa4fd.md": idx,
        f"{cat} 2 b8d52d73b.md": idx,
        f"{cat}/Airfrier da somon 2 d9c858f2c888f8.md": shot,
        f"{cat}/Airfrier da somon/IMG_5485.png": PNG,
    })
    r = client.post("/recipes/import-notion", json={"zip": zip_b64, "who": "owner"})
    assert r.status_code == 200, r.text
    names = {x["name"] for x in r.json()["imported"]}
    assert "Airfrier da somon" in names                    # recipe imported by vision
    shot_rec = storage.read_recipe_file(
        next(x["id"] for x in r.json()["imported"] if x["name"] == "Airfrier da somon"))
    assert shot_rec["photos"]                               # screenshot kept as the photo


def test_import_handles_nested_part_zip(client, monkeypatch):
    """Large Notion exports arrive as a zip-of-zips — the importer must recurse."""
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")

    async def fake_complete(system, messages, max_tokens, *, action, timeout=45):
        return json.dumps({"name": "Nested", "ingredients": [{"item": "egg", "amount": "1", "unit": ""}],
                           "steps": [{"text": "bake"}]})
    monkeypatch.setattr(ai, "complete", fake_complete)

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("Export/Recipes/Nested Recipe abc.md",
                   "# Nested Recipe\n\nA nested one. " + "Mix and bake for a while. " * 8)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("Part-1.zip", inner.getvalue())

    r = client.post("/recipes/import-notion",
                    json={"zip": base64.b64encode(outer.getvalue()).decode(), "who": "owner"})
    assert r.status_code == 200, r.text
    assert any(x["name"] == "Nested" for x in r.json()["imported"])


def test_import_routes_screenshot_to_vision_and_keeps_photo(client, monkeypatch):
    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")
    seen = []

    async def fake_complete(system, messages, max_tokens, *, action, timeout=45):
        content = messages[0]["content"]
        has_img = isinstance(content, list) and any(b.get("type") == "image" for b in content)
        seen.append(has_img)
        return json.dumps({
            "name": "From Screenshot" if has_img else "From Text",
            "ingredients": [{"item": "egg", "amount": "2", "unit": ""}],
            "steps": [{"text": "cook"}],
        })
    monkeypatch.setattr(ai, "complete", fake_complete)

    zip_b64 = _zip_b64({
        # screenshot page: title + image only (thin prose) → vision + photo
        "Export/Recipes/Grandma Pasta abc123.md":
            "# Grandma Pasta\n\n![s](Grandma%20Pasta%20abc123/shot.png)\n",
        "Export/Recipes/Grandma Pasta abc123/shot.png": PNG,
        # text page: real prose → text extraction, no image
        "Export/Recipes/Simple Soup def456.md":
            "# Simple Soup\n\nA cosy soup. " + "Simmer onions, carrots and stock. " * 8,
    })
    r = client.post("/recipes/import-notion", json={"zip": zip_b64, "who": "owner"})
    assert r.status_code == 200, r.text
    names = {x["name"] for x in r.json()["imported"]}
    assert names == {"From Screenshot", "From Text"}
    assert True in seen and False in seen                  # both vision and text paths used

    shot = next(x for x in r.json()["imported"] if x["name"] == "From Screenshot")
    rec = storage.read_recipe_file(shot["id"])
    assert rec["photos"] and rec["photos"][0]["path"].startswith(f"photos/{shot['id']}/")
