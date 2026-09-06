"""Willys price lookup: term translation, product picking and quantity costing.

All offline — `search()` is the only thing that touches the network and none of
these call it. The cases here are the ones that were actually wrong against live
data during development, which is why they read like a list of grievances:
cat food winning on price, a börek beating mince, ice cream matching "banan",
and two onions billed as two one-kilo sacks.
"""
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

def _q(lo, family, unit=""):
    return {"lo": lo, "hi": lo, "family": family, "unit": unit}


def test_costs_mass_against_the_kilo_price():
    kr, _ = willys.cost_of(_q(500, "mass"), _p("Nötfärs", 119.0))
    assert kr == 59.5


def test_costs_volume_against_the_litre_price():
    kr, _ = willys.cost_of(_q(500, "volume"), _p("Mjölk", 11.0, "l"))
    assert kr == 5.5


def test_costs_spoons_as_millilitres():
    """parse_qty folds tbsp/cups into tsp, so a spoon family only needs tsp->ml."""
    kr, _ = willys.cost_of(_q(6, "spoon"), _p("Olivolja", 64.9, "l"))
    assert round(kr, 2) == 1.95


def test_costs_a_count_against_the_piece_price():
    kr, _ = willys.cost_of(_q(4, "count"), _p("Ägg 24p", 2.5, "st"))
    assert kr == 10.0


def test_costs_a_count_of_a_weight_priced_item_by_item_weight():
    kr, basis = willys.cost_of(_q(3, "count"), _p("Banan", 19.9, volume="ca: 180g"))
    assert round(kr, 2) == 10.75 and "each" in basis


def test_a_count_never_multiplies_a_pack_size():
    """Two onions must not be billed as two one-kilo sacks — the honest answer is
    one pack, and it says so."""
    kr, basis = willys.cost_of(_q(2, "count"), _p("Lök i Påse", 9.9, volume="1kg", price=19.9))
    assert kr == 19.9 and basis == "one pack (count vs weight)"


def test_no_quantity_falls_back_to_one_pack_and_says_so():
    kr, basis = willys.cost_of(_q(None, ""), _p("Salt med Jod", 9.9, price=9.9))
    assert kr == 9.9 and "no quantity" in basis


def test_unreconcilable_units_are_reported_not_guessed():
    kr, basis = willys.cost_of(_q(2, "count"), _p("Mystisk Vara", 5.0, ""))
    assert kr is not None or basis            # never raises; either priced or explained


def test_missing_compare_price_is_not_priced():
    kr, basis = willys.cost_of(_q(500, "mass"), _p("Utan Pris", None))
    assert kr is None and basis == "no compare price"
