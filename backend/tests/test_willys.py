"""Willys price lookup: term translation, product picking and quantity costing.

All offline — `search()` is the only thing that touches the network and none of
these call it. The cases here are the ones that were actually wrong against live
data during development, which is why they read like a list of grievances:
cat food winning on price, a börek beating mince, ice cream matching "banan",
and two onions billed as two one-kilo sacks.
"""
import httpx
import pytest

import willys
from willys import Product


def _p(name, compare_price, compare_unit="kg", *, volume="", price=10.0, oos=False):
    return Product(code="x", name=name, manufacturer="", price=price,
                   compare_price=compare_price, compare_unit=compare_unit,
                   display_volume=volume, out_of_stock=oos)


# ── parsing ───────────────────────────────────────────────────────────────────

def test_kr_reads_the_swedish_decimal_comma():
    assert willys._kr("155,00 kr") == 155.0
    assert willys._kr("9,90 kr") == 9.9
    assert willys._kr(16.5) == 16.5
    assert willys._kr("") is None


def test_per_piece_grams_only_trusts_a_ca_label():
    """'ca: 180g' is the weight of one banana; '1kg' is the size of a sack. Only
    the former may be multiplied by a count."""
    assert _p("Banan", 19.9, volume="ca: 180g").per_piece_grams == 180.0
    assert _p("Lök i Påse", 9.9, volume="1kg").per_piece_grams is None
    assert _p("Lök i Påse", 9.9, volume="1kg").pack_grams == 1000.0


# ── translation ───────────────────────────────────────────────────────────────

def test_translates_english_and_turkish():
    assert willys.swedish_term("yumurta") == ("ägg", True)
    assert willys.swedish_term("tereyağı") == ("smör", True)
    assert willys.swedish_term("olive oil") == ("olivolja", True)


def test_qualifier_is_kept_when_it_changes_the_product():
    """'ground beef' is nötfärs; 'beef' is nötkött. Stripping 'ground' as a mere
    qualifier before lookup prices a roasting joint as mince."""
    assert willys.swedish_term("ground beef") == ("nötfärs", True)
    assert willys.swedish_term("beef") == ("nötkött", True)


def test_qualifier_is_dropped_when_it_is_only_decoration():
    assert willys.swedish_term("fresh parsley") == ("persilja", True)
    assert willys.swedish_term("finely chopped onion") == ("gul lök", True)


def test_head_phrase_and_trailing_noun_are_tried():
    assert willys.swedish_term("olive oil, extra virgin") == ("olivolja", True)
    assert willys.swedish_term("san marzano tomato") == ("tomat", True)


def test_unknown_terms_are_searched_verbatim_but_flagged():
    term, translated = willys.swedish_term("gochujang")
    assert (term, translated) == ("gochujang", False)


def test_turkish_compounds_resolve_from_their_head_word():
    """Turkish noun compounds are head-INITIAL, so 'tavuk göğüs' (chicken breast)
    has to fall back to the first word where English falls back to the last."""
    assert willys.swedish_term("haşlanmış Tavuk Göğüs") == ("kycklingfilé", True)


def test_water_and_ice_are_free_however_they_are_qualified():
    assert willys.is_free("water") and willys.is_free("su") and willys.is_free("ılık su")
    assert willys.is_free("kaynamış su") and willys.is_free("buz")
    assert not willys.is_free("sparkling water")


# ── picking ───────────────────────────────────────────────────────────────────

def test_rank_prefers_the_head_noun():
    assert willys._rank("nötfärs", "Nötfärs 12% Sverige") == 0
    assert willys._rank("nötfärs", "Burek Nötfärs Fryst") == 1
    assert willys._rank("nötfärs", "Kycklingfilé") == 2


def test_rank_understands_head_final_compounds():
    """bladpersilja IS a parsley; a bearnaise merely mentions one."""
    assert willys._rank("persilja", "Bladpersilja Lösvikt") == 0
    assert willys._rank("persilja", "Bearnaise Dragon & Persilja") == 1


def test_rank_does_not_match_a_longer_word():
    assert willys._rank("banan", "Banana Split Glass") == 2


def test_pick_rejects_pet_food_however_cheap():
    """Cat mince is the cheapest thing called 'nötfärs' in the store."""
    got = willys.pick("nötfärs", [
        _p("Nötfärs Bitar i Gelé Kattmat", 38.16),
        _p("Nötfärs 12% Sverige", 159.80),
    ])
    assert got.name == "Nötfärs 12% Sverige"


def test_pick_prefers_the_real_article_over_a_cheaper_impostor():
    got = willys.pick("nötfärs", [
        _p("Burek Nötfärs Fryst", 78.39),
        _p("Nötfärs 20% Irland", 119.0),
    ])
    assert got.name == "Nötfärs 20% Irland"


def test_pick_takes_the_cheapest_of_equally_good_matches():
    got = willys.pick("ris", [_p("Ris Basmati", 29.9), _p("Ris Långkornigt", 14.95)])
    assert got.name == "Ris Långkornigt"


def test_pick_ignores_absurdly_cheap_bad_data():
    """Willys lists a jar of dried parsley at 1,00 kr/kg. Believing it makes a
    bunch of parsley cost two öre and quietly deflates the whole basket."""
    got = willys.pick("persilja", [
        _p("Persilja Burk", 1.0),
        _p("Bladpersilja Lösvikt", 49.9),
        _p("Persilja Kruka", 59.0),
    ])
    assert got.name == "Bladpersilja Lösvikt"


def test_pick_prefers_a_per_item_weight_when_the_recipe_counts():
    loose, sack = _p("Lök Gul Klass 1", 10.9, volume="ca: 175g"), _p("Lök Gul i Påse", 9.9, volume="1kg")
    assert willys.pick("gul lök", [sack, loose], want_count=True).name == "Lök Gul Klass 1"
    assert willys.pick("gul lök", [sack, loose]).name == "Lök Gul i Påse"   # cheapest per kg otherwise


def test_pick_skips_out_of_stock():
    assert willys.pick("ris", [_p("Ris Billigt", 5.0, oos=True), _p("Ris Långkornigt", 14.95)]).name \
        == "Ris Långkornigt"


def test_pick_returns_none_when_there_is_nothing_to_pick():
    assert willys.pick("ris", []) is None


def test_strict_refuses_to_guess_for_an_untranslated_term():
    """'ezilmiş sarmısak' went to the store verbatim and came back as a 195 kr box
    of chocolates. Unpriced is a better answer than confidently wrong."""
    junk = [_p("Sorte Sara 25%", 195.0), _p("Kexchoklad", 120.0), _p("Marabou", 99.0)]
    assert willys.pick("ezilmiş sarmısak", junk, strict=True) is None
    assert willys.pick("ezilmiş sarmısak", junk) is not None      # lenient still guesses


# ── costing ───────────────────────────────────────────────────────────────────
# Basket cost, not consumption cost. A recipe using 20 g of flour does not cost
# 13 öre; it costs a bag of flour, because that is what you carry to the till.

def _q(lo, family, unit=""):
    return {"lo": lo, "hi": lo, "family": family, "unit": unit}


def test_packaged_goods_are_charged_as_a_whole_pack():
    """You cannot buy 300 g out of a 2 kg bag."""
    flour = _p("Vetemjöl", 12.5, volume="2kg", price=25.0)
    kr, basis = willys.cost_of(_q(300, "mass"), flour)
    assert kr == 25.0 and "1 pack" in basis


def test_more_than_one_pack_when_the_recipe_needs_more():
    flour = _p("Vetemjöl", 12.5, volume="2kg", price=25.0)
    kr, basis = willys.cost_of(_q(3000, "mass"), flour)
    assert kr == 50.0 and basis.startswith("2 ×")


def test_an_exact_multiple_does_not_round_up_a_spare_pack():
    """2000 g against a 2 kg bag is one bag, not two — float noise must not add one."""
    kr, _ = willys.cost_of(_q(2000, "mass"), _p("Vetemjöl", 12.5, volume="2kg", price=25.0))
    assert kr == 25.0


def test_loose_produce_is_charged_for_what_you_take():
    """Nobody sells three onions in a sealed bag; they are weighed at the till."""
    onion = _p("Lök Gul Klass 1", 10.9, volume="ca: 175g", price=8.9)
    kr, basis = willys.cost_of(_q(2, "count"), onion)
    assert round(kr, 2) == 3.81 and basis.startswith("loose")


def test_loose_by_weight_is_charged_by_weight():
    mince = _p("Nötfärs", 155.0, volume="ca: 850g", price=131.0)
    kr, basis = willys.cost_of(_q(500, "mass"), mince)
    assert round(kr, 2) == 77.5 and basis.startswith("loose")


def test_a_count_of_packaged_pieces_uses_the_pack_count():
    """Four eggs means one box of 24, not four twenty-fourths of one."""
    eggs = _p("Ägg 24p Frigående", 2.5, "st", volume="24p", price=59.9)
    kr, basis = willys.cost_of(_q(4, "count"), eggs)
    assert kr == 59.9 and "1 pack" in basis


def test_a_count_beyond_one_pack_buys_two():
    eggs = _p("Ägg 24p Frigående", 2.5, "st", volume="24p", price=59.9)
    kr, _ = willys.cost_of(_q(30, "count"), eggs)
    assert kr == 119.8


def test_spoons_become_millilitres_then_a_pack():
    """6 tsp of oil is 30 ml, and 30 ml of oil is one bottle of oil."""
    oil = _p("Olivolja", 64.9, "l", volume="1l", price=64.9)
    kr, basis = willys.cost_of(_q(6, "spoon"), oil)
    assert kr == 64.9 and "1 pack" in basis


def test_no_quantity_falls_back_to_one_pack_and_says_so():
    kr, basis = willys.cost_of(_q(None, ""), _p("Salt med Jod", 9.9, volume="1kg", price=9.9))
    assert kr == 9.9 and "no quantity" in basis


def test_unknown_pack_size_falls_back_to_one_pack():
    kr, basis = willys.cost_of(_q(300, "mass"), _p("Mystisk Vara", 20.0, volume="", price=17.0))
    assert kr == 17.0 and "pack size unknown" in basis


def test_a_count_of_a_packaged_weight_item_is_one_pack():
    """'2 packs of mince' against a sealed 500 g tray: buy the tray."""
    kr, basis = willys.cost_of(_q(2, "count"), _p("Nötfärs", 159.8, volume="500g", price=79.9))
    assert kr == 79.9 and "count vs weight" in basis


# ── picking follows the basket, not the unit price ────────────────────────────

def test_pick_buys_the_smallest_bag_that_covers_the_need():
    """Cheapest per kilo is a bulk rule: it buys a 5 kg sack to satisfy 300 g."""
    small = _p("Vetemjöl Liten", 12.5, volume="2kg", price=25.0)
    bulk  = _p("Vetemjöl Stor",   9.0,  volume="5kg", price=45.0)
    got = willys.pick("vetemjöl", [bulk, small], qty=_q(300, "mass"))
    assert got.name == "Vetemjöl Liten"


def test_pick_buys_the_sack_once_the_sack_is_genuinely_cheaper():
    small = _p("Vetemjöl Liten", 12.5, volume="2kg", price=25.0)
    bulk  = _p("Vetemjöl Stor",   9.0,  volume="5kg", price=45.0)
    got = willys.pick("vetemjöl", [bulk, small], qty=_q(3000, "mass"))
    assert got.name == "Vetemjöl Stor"          # 1×45 beats 2×25


# ── what the cart will actually accept ────────────────────────────────────────

def test_a_kg_code_suffix_does_not_mean_bought_by_the_kilo():
    """101203622_KG is "Nötfärs 20% Irland": a variable-weight ~1 kg pack you take
    ONE of and have weighed at the till. The suffix describes how it is priced;
    productBasketType says how it is bought, and ordering kilos of it is refused."""
    raw = {"code": "101203622_KG", "name": "Nötfärs 20% Irland",
           "productBasketType": {"code": "ST"}, "priceValue": 119.0,
           "comparePrice": "119,00 kr", "comparePriceUnit": "kg",
           "displayVolume": "ca: 1kg", "incrementValue": 1.0}
    assert willys._product(raw).pick_unit == "pieces"


def test_genuinely_weight_bought_products_still_ask_for_kilos():
    raw = {"code": "12345_KG", "productBasketType": {"code": "KG"}, "incrementValue": 0.1}
    assert willys._product(raw).pick_unit == "kilogram"


def test_the_quantity_lands_on_the_step_the_product_is_sold_in():
    """A quantity off `incrementValue` is refused as an illegal argument with no
    hint as to why — mince comes in whole ~1 kg packs, not 0.196 of one."""
    mince = Product(code="101203622_KG", name="Nötfärs", manufacturer="", price=119.0,
                    compare_price=119.0, compare_unit="kg", display_volume="ca: 1kg",
                    out_of_stock=False, basket_type="ST", increment=1.0)
    assert willys.plan(_q(300, "mass"), mince).units == 1.0

    loose = Product(code="9_KG", name="Fläskfärs", manufacturer="", price=50.0,
                    compare_price=50.0, compare_unit="kg", display_volume="ca: 500g",
                    out_of_stock=False, basket_type="KG", increment=0.1)
    assert willys.plan(_q(250, "mass"), loose).units == 0.3


def test_pieces_are_whole_numbers():
    """Half a cucumber is not a thing you can put in a trolley."""
    line = willys.plan(_q(0.5, "count"), _p("Gurka", 20.0, volume="ca: 100g", price=10.0))
    assert line.pick_unit == "pieces" and line.units == 1.0


def test_rounding_up_a_piece_moves_the_price_with_it():
    """Buying a whole cucumber to satisfy half of one costs a whole cucumber, and
    the estimate has to say so — or it stops matching the till."""
    half  = willys._plan_raw(_q(0.5, "count"), _p("Gurka", 20.0, volume="ca: 100g", price=10.0))
    whole = willys.plan(_q(0.5, "count"), _p("Gurka", 20.0, volume="ca: 100g", price=10.0))
    assert whole.units == 2 * half.units
    assert round(whole.kr, 4) == round(2 * half.kr, 4)


def test_a_weight_line_is_never_below_the_shop_minimum():
    """50 g of anything is refused; the floor is a tenth of a kilo."""
    p = Product(code="1_KG", name="Gurka", manufacturer="", price=10.0, compare_price=20.0,
                compare_unit="kg", display_volume="ca: 100g", out_of_stock=False, basket_type="KG")
    line = willys.plan(_q(0.5, "count"), p)
    assert line.pick_unit == "kilogram" and line.units >= 0.1


# ── store scoping ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_search_is_cached_per_assortment(anyio_backend, monkeypatch, tmp_path):
    """The store and the national catalogue answer the same query with different
    products at different prices, so one cache entry cannot serve both — the
    anonymous answer would be handed to the cart, which rejects codes its store
    has never heard of."""
    monkeypatch.setattr(willys, "_CACHE_FILE", tmp_path / "c.json")
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        code = "101895176_ST" if request.headers.get("cookie") else "100261164_ST"
        return httpx.Response(200, json={"results": [
            {"code": code, "name": "Vispgrädde 40%", "priceValue": 17.9,
             "comparePrice": "59,67 kr", "comparePriceUnit": "l", "displayVolume": "3dl"}]})

    cache = {}
    async with httpx.AsyncClient(base_url=willys.BASE,
                                 transport=httpx.MockTransport(handler)) as c:
        anon = await willys.search("vispgrädde", client=c, cache=cache)
        store = await willys.search("vispgrädde", client=c, cache=cache, cookie="JSESSIONID=x")
        again = await willys.search("vispgrädde", client=c, cache=cache, cookie="JSESSIONID=x")

    assert anon[0].code == "100261164_ST"
    assert store[0].code == "101895176_ST"      # not served the anonymous answer
    assert again[0].code == store[0].code
    assert len(seen) == 2                       # the repeat came from cache


def test_average_weight_beats_parsing_the_label():
    """`averageWeight` states the per-item weight outright — 0.098 kg for a
    tomato, 1.0 kg for a pack of mince — and needs no `ca:` heuristic or cap."""
    raw = {"code": "101203622_KG", "productBasketType": {"code": "ST"},
           "averageWeight": 1.0, "displayVolume": "ca: 1kg", "priceValue": 119.0,
           "comparePrice": "119,00 kr", "comparePriceUnit": "kg"}
    assert willys._product(raw).per_piece_grams == 1000.0     # not capped away


def test_loose_weight_is_charged_for_what_is_ordered():
    """500 g of tomatoes is six tomatoes weighing 588 g, and the till charges for
    588 g. Pricing the 500 g the recipe asked for makes the estimate disagree with
    the cart it just filled."""
    tomato = Product(code="100521259_KG", name="Tomat", manufacturer="", price=59.9,
                     compare_price=59.9, compare_unit="kg", display_volume="ca: 98g",
                     out_of_stock=False, basket_type="ST", increment=1.0,
                     average_weight=0.098)
    line = willys.plan(_q(500, "mass"), tomato)
    assert line.units == 6.0
    assert round(line.kr, 2) == round(6 * 0.098 * 59.9, 2) == 35.22


def test_a_one_kilo_pack_is_one_pack_not_a_fraction_of_need():
    """1.25 kg of mince is two ~1 kg packs, and costs two."""
    mince = Product(code="101203622_KG", name="Nötfärs", manufacturer="", price=119.0,
                    compare_price=119.0, compare_unit="kg", display_volume="ca: 1kg",
                    out_of_stock=False, basket_type="ST", increment=1.0, average_weight=1.0)
    line = willys.plan(_q(1250, "mass"), mince)
    assert line.units == 2.0 and round(line.kr, 2) == 238.0
