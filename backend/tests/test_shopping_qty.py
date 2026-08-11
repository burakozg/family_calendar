"""F1: parse_qty() amount normalization and its presence in the shopping
payload. Quantities are folded into a family base unit (mass→g, volume→ml,
spoons/cups→tsp, count) so the client can sum them across the selected days."""
import pytest

import shopping
from shopping import parse_qty, _shopping_payload


@pytest.mark.parametrize("amount,unit,expect", [
    # empty / no amount
    ("", "",        {"lo": None, "hi": None, "family": "",      "unit": ""}),
    ("", "g",       {"lo": None, "hi": None, "family": "",      "unit": ""}),
    # plain numbers, decimal point and decimal comma
    ("3", "",       {"lo": 3.0,  "hi": 3.0,  "family": "count", "unit": ""}),
    ("2.5", "",     {"lo": 2.5,  "hi": 2.5,  "family": "count", "unit": ""}),
    ("2,5", "",     {"lo": 2.5,  "hi": 2.5,  "family": "count", "unit": ""}),
    # fractions and mixed numbers
    ("1/2", "",     {"lo": 0.5,  "hi": 0.5,  "family": "count", "unit": ""}),
    ("1 1/2", "",   {"lo": 1.5,  "hi": 1.5,  "family": "count", "unit": ""}),
    # ranges (ascii hyphen and en dash)
    ("1-2", "",     {"lo": 1.0,  "hi": 2.0,  "family": "count", "unit": ""}),
    ("1–2", "",     {"lo": 1.0,  "hi": 2.0,  "family": "count", "unit": ""}),
    # mass → grams
    ("200", "g",    {"lo": 200.0,  "hi": 200.0,  "family": "mass",   "unit": ""}),
    ("1", "kg",     {"lo": 1000.0, "hi": 1000.0, "family": "mass",   "unit": ""}),
    # volume → ml
    ("2", "dl",     {"lo": 200.0,  "hi": 200.0,  "family": "volume", "unit": ""}),
    ("1", "l",      {"lo": 1000.0, "hi": 1000.0, "family": "volume", "unit": ""}),
    # spoons/cups → tsp
    ("1", "tbsp",   {"lo": 3.0,  "hi": 3.0,  "family": "spoon",  "unit": ""}),
    ("1", "cup",    {"lo": 48.0, "hi": 48.0, "family": "spoon",  "unit": ""}),
    # count with a size word (word kept as the display token)
    ("2", "small",  {"lo": 2.0,  "hi": 2.0,  "family": "count",  "unit": "small"}),
    ("3", "cloves", {"lo": 3.0,  "hi": 3.0,  "family": "count",  "unit": "clove"}),
    # unparseable amount, or unknown unit → not summable
    ("a pinch", "", {"lo": None, "hi": None, "family": "",       "unit": ""}),
    ("1", "pinch",  {"lo": None, "hi": None, "family": "",       "unit": ""}),
])
def test_parse_qty(amount, unit, expect):
    assert parse_qty(amount, unit) == expect


def test_parse_qty_unit_case_and_trailing_dot():
    assert parse_qty("2", "KG")["family"] == "mass"
    assert parse_qty("1", "tsp.")["family"] == "spoon"


def _seed(monkeypatch, plan, recipes):
    monkeypatch.setattr(shopping, "read_meals", lambda: {"plan": {"2026-40": plan}})
    monkeypatch.setattr(shopping, "read_shopping", lambda: {})
    monkeypatch.setattr(shopping, "read_recipe_file", lambda rid: recipes.get(rid))


def test_payload_includes_qty_and_keeps_raw(monkeypatch):
    _seed(monkeypatch,
          plan=[{"id": "r1", "name": "Soup"}],
          recipes={"r1": {"ingredients": [
              {"item": "Carrot", "amount": "200", "unit": "g", "category": "Produce"},
              {"item": "Salt",   "amount": "",    "unit": ""},
          ]}})
    ing = _shopping_payload("2026-40")["days"][0]["ingredients"]
    carrot = ing[0]
    assert carrot["amount"] == "200" and carrot["unit"] == "g"      # raw untouched
    assert carrot["qty"] == {"lo": 200.0, "hi": 200.0, "family": "mass", "unit": ""}
    assert ing[1]["qty"] == {"lo": None, "hi": None, "family": "", "unit": ""}


def test_payload_includes_extras_with_category(monkeypatch):
    # F9: manual extras ride along, tagged with an aisle; blanks are skipped.
    monkeypatch.setattr(shopping, "read_meals", lambda: {"plan": {}})
    monkeypatch.setattr(shopping, "read_shopping", lambda: {"2026-40": {"extras": [
        {"item": "Batteries", "who": "owner"}, {"item": "  Milk  "}, {"item": "   "}, "junk",
    ]}})
    extras = _shopping_payload("2026-40")["extras"]
    assert [e["item"] for e in extras] == ["Batteries", "Milk"]     # trimmed, blanks/non-dicts dropped
    assert extras[0]["who"] == "owner"
    assert extras[1]["category"] == "Dairy & Eggs"                  # keyword-tagged aisle
