"""F14: on-demand data + recipes export downloads (zip)."""
import io
import zipfile

import storage


def test_export_all_data_zip(client):
    r = client.get("/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and cd.endswith('.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "settings.json" in names and "events.json" in names
    assert z.testzip() is None
    # The full backup is deliberately photo-free (bulky/reproducible).
    assert not any(n.startswith("recipes/photos/") for n in names)


def test_export_recipes_zip_includes_index_and_photos(client):
    storage.F_RECIPE_IDX.write_text('[{"id":"rx","name":"Soup"}]')
    (storage.RECIPES_DIR / "rx.json").write_text('{"id":"rx","name":"Soup"}')
    storage.PHOTOS_DIR.mkdir(exist_ok=True)
    (storage.PHOTOS_DIR / "rx.jpg").write_bytes(b"\xff\xd8\xff-photo")
    r = client.get("/export/recipes")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "recipes/index.json" in names
    assert "recipes/rx.json" in names
    assert "recipes/photos/rx.jpg" in names           # photos travel with recipes
    # A recipes bundle carries no top-level calendar stores.
    assert "settings.json" not in names and "events.json" not in names
    assert z.testzip() is None
