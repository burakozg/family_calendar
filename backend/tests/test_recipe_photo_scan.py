"""The phone photo scan (relay `recipe_photo` command → _import_recipe_photo).
It shares the vision extractor with the Notion import, which used to cap the
reply at 2000 tokens — half what the home editor's photo route allows. A printed
recipe card (≈18 ingredients, 6 steps, every field provenance-wrapped) overran
that cap and came back as truncated JSON, so the scan failed on exactly the
recipes worth scanning."""
import ai
import pytest
import recipes
import storage


@pytest.fixture(autouse=True)
def clean_recipes(client):
    for p in storage.RECIPES_DIR.glob("*.json"):
        if p.name != storage.F_RECIPE_IDX.name:
            p.unlink()
    storage.write_recipe_index([])
    yield


DRAFT = """{"name": {"value": "Ugnsstek kyckling", "source": "extracted"},
 "source": {"type": {"value": "photo", "source": "extracted"},
            "value": {"value": "", "source": "empty"}},
 "tags": [{"value": "kyckling", "source": "extracted"}],
 "ingredients": [{"item": {"value": "potatis", "source": "extracted"},
                  "amount": {"value": "400", "source": "extracted"},
                  "unit": {"value": "g", "source": "extracted"}}],
 "steps": [{"text": {"value": "Rosta potatis.", "source": "extracted"}}]}"""


@pytest.fixture()
def captured_ai(monkeypatch):
    """Stand in for the vision call, recording how it was invoked."""
    calls = []

    async def fake_complete(system, messages, max_tokens, *, action, timeout=45):
        calls.append({"max_tokens": max_tokens, "action": action})
        return DRAFT

    monkeypatch.setattr(ai, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(ai, "complete", fake_complete)
    return calls


async def _scan(payload):
    await recipes._import_recipe_photo(payload)


@pytest.mark.anyio
async def test_photo_scan_saves_a_reviewable_recipe(captured_ai, anyio_backend):
    await _scan({"image": _one_px_jpeg_b64(), "media": "image/jpeg", "who": "burak"})

    idx = storage.read_recipe_index()
    assert [e["id"] for e in idx] == ["ugnsstek-kyckling"]
    saved = storage.read_recipe_file("ugnsstek-kyckling")
    assert saved["needs_review"] is True
    assert saved["tags"] == ["kyckling"]                      # unwrapped, not dicts
    assert saved["ingredients"][0]["item"] == "potatis"
    assert saved["log"]["entered_by"] == "burak"


@pytest.mark.anyio
async def test_photo_scan_gets_the_full_token_budget(captured_ai, anyio_backend):
    """A tighter cap here than on /recipes/extract truncates the reply mid-JSON."""
    await _scan({"image": _one_px_jpeg_b64(), "media": "image/jpeg", "who": ""})

    assert captured_ai[0]["max_tokens"] == recipes.EXTRACT_MAX_TOKENS
    assert captured_ai[0]["action"] == "recipe.photo"          # not "recipe.import_notion"


def _one_px_jpeg_b64() -> str:
    """Smallest valid JPEG — the extractor is mocked, only base64 validity matters."""
    import base64
    return base64.b64encode(bytes.fromhex(
        "ffd8ffdb004300ff" "ffc2000b080001000101011100"
        "ffc40014000100000000000000000000000000000009"
        "ffda0008010100000001d2cf20ffd9")).decode()
