"""Willys (Axfood) price lookup — the READ-ONLY half of the grocery integration.

What this is for: costing a planned week before you shop, so the shopping page
can say "this list is about 780 kr at Willys" and which items it couldn't price.

Deliberately read-only and anonymous. No account, no personnummer, no password,
no session cookie — this module only ever issues GETs against a public search
endpoint. That is the whole reason it is safe to run on a schedule: the worst a
break can do is leave the estimate blank. Anything that *writes* (a cart, an
order) is a separate module with a completely different risk profile, and this
one must never grow those verbs.

The endpoint was found from the site's own config rather than a third-party
package: `GET https://www.willys.se/api/config` publishes `API_URL`, and the
Next.js chunks enumerate `axfood/rest/...`. The one we want is

    GET /axfood/rest/v1/search?q=<term>&size=<n>     -> {"results": [ ... ]}

Note `/axfood/rest/p/{code}` is a product-by-code lookup, NOT a text search — it
answers 400 "No product found" for a word, which reads like a broken endpoint
and is the wrong turn to take twice.

Two fields make this tractable, and they are the reason costing is honest rather
than a guess:

  * `comparePrice` + `comparePriceUnit` — the jämförpris, i.e. kr/kg, kr/l or
    kr/st. It is legally mandated, so it is present, consistent, and directly
    comparable ACROSS products and (later) across chains. We cost against this,
    never against the shelf price, so a 500 g pack and a 1 kg pack of the same
    mince produce the same per-gram answer.
  * `priceValue` — already a float, so no Swedish decimal comma to parse. Every
    *other* money field is a string like "155,00 kr"; `_kr()` handles those.

Matching is the accuracy risk, not the HTTP. Recipe ingredients here are written
in English and Turkish and the store speaks Swedish, so a term has to be
translated before it is searched (`_SV` below), and the search results then have
to be filtered — a query for "banan" happily returns a banana-flavoured baby
snack at 517 kr/kg. Anything we cannot translate or match is reported as
unmatched rather than quietly priced wrong; a total that silently omits items is
worse than one that says what it missed.

When a second chain is added, `_SV`, `pick()` and `cost_of()` lift out into a
shared matching module and this file keeps only the HTTP client.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import NamedTuple

import httpx

from storage import CACHE
from fsatomic import _atomic_write_text

log = logging.getLogger(__name__)

BASE        = "https://www.willys.se"
SEARCH_PATH = "/axfood/rest/v1/search"
TIMEOUT_S   = 15.0

# A real browser UA. Not an attempt to evade anything — the endpoint is public and
# unauthenticated — but a default python-httpx UA is the kind of thing that gets
# null-routed by a WAF long before a human notices, and this runs unattended.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Prices move at most weekly (campaigns flip Monday), so a long TTL keeps the
# request count near zero: a full 30-item list costs 30 GETs once, then nothing
# for half a day. Being quiet is also the best bot-detection posture there is.
CACHE_TTL_S = 12 * 3600
_CACHE_FILE = CACHE / "willys_search.json"


# Heaviest thing a recipe plausibly counts one of. Above this a "ca:" label is a
# variable-weight pack, not an item — see Product.per_piece_grams.
_MAX_PIECE_G = 400.0


class WillysUnavailable(Exception):
    """Willys could not be reached, or answered something we can't read."""


# ── response model ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Product:
    code: str
    name: str
    manufacturer: str
    price: float | None           # shelf price of one pack, kr
    compare_price: float | None   # jämförpris, kr per compare_unit
    compare_unit: str             # 'kg' | 'l' | 'st' | ''
    display_volume: str           # '500g', 'ca: 180g', '1,5l', ''
    out_of_stock: bool
    basket_type: str = "ST"       # 'ST' (bought by the piece) | 'KG' (by weight)
    increment: float = 1.0        # the step the cart accepts for this product

    @property
    def pick_unit(self) -> str:
        """What the cart calls this product's unit: 'pieces' or 'kilogram'.

        The storefront's own rule, and not a synonym for how the price is quoted:
        a banana is priced per kilo but bought by the piece, so it is ST/pieces.
        """
        return "kilogram" if self.basket_type.upper() == "KG" else "pieces"

    @property
    def pack_grams(self) -> float | None:
        """Pack size in grams, when the label states one ('500g', '1,5l')."""
        return _grams(self.display_volume)

    @property
    def per_piece_grams(self) -> float | None:
        """Weight of ONE item, and only when the label actually means one.

        Willys writes an indicative single-item weight as "ca: 180g" (a banana, a
        garlic bulb) and a pack size as a bare "500g" / "1kg". They must not be
        confused: costing "2 onions" against a 1 kg *bag* bills two whole bags.

        "ca:" alone isn't enough, though — a variable-weight MEAT pack carries it
        too ("ca: 850g" of chicken fillets), and reading that as one piece charges
        four times over for a single breast. Nothing sold by the piece weighs much
        more than `_MAX_PIECE_G`, and above it "one pack" is the better answer
        anyway, so the cap costs nothing and removes the whole failure mode.
        """
        if not self.is_loose:
            return None
        g = _grams(self.display_volume)
        return g if g is not None and g <= _MAX_PIECE_G else None

    @property
    def is_loose(self) -> bool:
        """Sold loose by weight rather than in a sealed pack.

        Willys marks these with an approximate label ("ca: 180g") because what you
        take off the shelf is whatever it weighs. It decides how the thing is
        costed: loose produce is charged for the amount you actually need, a
        packaged good for whole packs, because you cannot buy 300 g out of a 2 kg
        bag of flour.
        """
        return bool(re.match(r"\s*ca\b", self.display_volume, re.I))

    @property
    def pack_count(self) -> float | None:
        """How many pieces are in the pack, from labels like '24p' (a box of eggs)."""
        m = re.search(r"(\d+)\s*(?:p|pack|st)\b", self.display_volume, re.I)
        return float(m.group(1)) if m else None


def _kr(s) -> float | None:
    """'155,00 kr' -> 155.0. Swedish decimal comma; None when unparseable."""
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(s or ""))
    return float(m.group(1).replace(",", ".")) if m else None


_VOL_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml)\b", re.I)
_VOL_TO_G = {"kg": 1000.0, "g": 1.0, "l": 1000.0, "dl": 100.0, "cl": 10.0, "ml": 1.0}


def _grams(label: str) -> float | None:
    """Pack label -> grams (millilitres treated as grams, density ~1).

    Only used for count-of-a-weight-priced-item costing ('2 bananas' where
    bananas are sold by the kilo), where an approximate density is fine.
    """
    m = _VOL_RE.search(label or "")
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * _VOL_TO_G[m.group(2).lower()]


def _basket_type(raw: dict) -> str:
    """'ST' (bought by the piece) or 'KG' (bought by weight).

    From `productBasketType`, which is what the storefront itself uses to choose
    a pick unit. The code SUFFIX is a trap: 101203622_KG is "Nötfärs 20% Irland",
    a variable-weight ~1 kg pack you take one of and have weighed at the till —
    its basket type is ST, and ordering it as kilograms is rejected outright.
    The suffix describes how it is priced, not how it is bought.
    """
    return str(((raw.get("productBasketType") or {}).get("code")) or "ST").upper()


def _product(raw: dict) -> Product:
    return Product(
        code           = str(raw.get("code") or ""),
        name           = str(raw.get("name") or ""),
        manufacturer   = str(raw.get("manufacturer") or ""),
        price          = _kr(raw.get("priceValue", raw.get("price"))),
        compare_price  = _kr(raw.get("comparePrice")),
        compare_unit   = str(raw.get("comparePriceUnit") or "").lower(),
        display_volume = str(raw.get("displayVolume") or ""),
        out_of_stock   = bool(raw.get("outOfStock")),
        basket_type    = _basket_type(raw),
        increment      = float(raw.get("incrementValue") or 1.0) or 1.0,
    )


# ── search ────────────────────────────────────────────────────────────────────

def _cache_read() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _cache_write(cache: dict) -> None:
    try:
        _atomic_write_text(_CACHE_FILE, json.dumps(cache, ensure_ascii=False))
    except Exception as e:                    # a cache that can't persist must not
        log.warning("willys: cache write failed: %s", e)   # break the lookup itself


async def search(term: str, *, size: int = 10, client: httpx.AsyncClient | None = None,
                 cache: dict | None = None) -> list[Product]:
    """Products matching `term`, best-relevance first. [] when nothing matched.

    `cache` lets a caller thread one dict through a whole list so the 12h store
    is read and written once per estimate instead of once per ingredient.
    """
    term = (term or "").strip()
    if not term:
        return []

    key = f"{term.lower()}|{size}"
    store = _cache_read() if cache is None else cache
    hit = store.get(key)
    if hit and (time.time() - hit.get("at", 0)) < CACHE_TTL_S:
        return [_product(r) for r in hit.get("results", [])]

    owned = client is None
    client = client or httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT_S,
                                         headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        r = await client.get(SEARCH_PATH, params={"q": term, "size": size})
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception as e:
        raise WillysUnavailable(f"search {term!r}: {e}") from e
    finally:
        if owned:
            await client.aclose()

    store[key] = {"at": time.time(), "results": results}
    if cache is None:
        _cache_write(store)
    return [_product(x) for x in results]


# ── ingredient -> Swedish search term ─────────────────────────────────────────
# Recipes here are written in English and Turkish; the store speaks Swedish. An
# unmapped term is still *tried* verbatim (loan words like tahini, bulgur,
# couscous, quinoa hit fine), but it is flagged so the estimate can say so.

_SV: dict[str, str] = {
    # staples
    "salt": "salt", "tuz": "salt",
    "black pepper": "svartpeppar", "pepper": "svartpeppar", "karabiber": "svartpeppar",
    "olive oil": "olivolja", "zeytinyağı": "olivolja", "zeytinyagi": "olivolja",
    "vegetable oil": "rapsolja", "neutral oil": "rapsolja", "sunflower oil": "solrosolja",
    "sıvı yağ": "rapsolja", "sivi yag": "rapsolja", "ayçiçek yağı": "solrosolja",
    "flour": "vetemjöl", "un": "vetemjöl", "all-purpose flour": "vetemjöl",
    "sugar": "strösocker", "şeker": "strösocker", "seker": "strösocker",
    "powdered sugar": "florsocker", "pudra şekeri": "florsocker",
    "vanilla sugar": "vaniljsocker", "vanilya şekeri": "vaniljsocker",
    "baking powder": "bakpulver", "kabartma tozu": "bakpulver",
    "baking soda": "bikarbonat", "karbonat": "bikarbonat",
    "yeast": "jäst", "maya": "jäst", "kuru maya": "torrjäst",
    "cornstarch": "majsstärkelse", "nişasta": "majsstärkelse",
    "vinegar": "ättika", "sirke": "ättika",
    "honey": "honung", "bal": "honung",
    "breadcrumbs": "ströbröd", "galeta unu": "ströbröd",
    "semolina": "mannagryn", "irmik": "mannagryn",
    "oats": "havregryn", "yulaf": "havregryn",
    "rice": "ris", "pirinç": "ris", "pirinc": "ris",
    "bulgur": "bulgur", "couscous": "couscous", "quinoa": "quinoa",
    "pasta": "pasta", "makarna": "pasta",
    "bread": "bröd", "ekmek": "bröd",
    "tahini": "tahini", "tahin": "tahini",
    # dairy & eggs
    "egg": "ägg", "eggs": "ägg", "yumurta": "ägg",
    "milk": "mjölk", "süt": "mjölk", "sut": "mjölk",
    "butter": "smör", "tereyağı": "smör", "tereyagi": "smör",
    "yogurt": "naturell yoghurt", "yoğurt": "naturell yoghurt",
    "cream": "vispgrädde", "whipping cream": "vispgrädde", "krema": "vispgrädde",
    "sour cream": "gräddfil", "ekşi krema": "gräddfil",
    "crème fraîche": "creme fraiche", "creme fraiche": "creme fraiche",
    "cheese": "ost", "peynir": "ost",
    "feta": "fetaost", "beyaz peynir": "fetaost",
    "cream cheese": "färskost",
    "parmesan": "parmesan", "mozzarella": "mozzarella",
    # produce
    "onion": "gul lök", "soğan": "gul lök", "sogan": "gul lök",
    "spring onion": "salladslök", "taze soğan": "salladslök", "taze sogan": "salladslök",
    "red onion": "rödlök", "kırmızı soğan": "rödlök",
    # "sarmısak" and "sarımsak" are both current spellings; recipes use both.
    "garlic": "vitlök", "sarımsak": "vitlök", "sarimsak": "vitlök", "sarmısak": "vitlök",
    "sarmisak": "vitlök",
    "leek": "purjolök", "pırasa": "purjolök",
    "tomato": "tomat", "tomatoes": "tomat", "domates": "tomat",
    "potato": "potatis", "patates": "potatis",
    "carrot": "morot", "havuç": "morot",
    "cucumber": "gurka", "salatalık": "gurka",
    "bell pepper": "paprika", "paprika": "paprika", "biber": "paprika",
    "chili": "chili", "acı biber": "chili",
    "eggplant": "aubergine", "patlıcan": "aubergine",
    "zucchini": "zucchini", "kabak": "zucchini",
    "mushroom": "champinjoner", "mantar": "champinjoner",
    "spinach": "spenat", "ıspanak": "spenat", "ispanak": "spenat",
    "lettuce": "sallad", "marul": "romansallad", "salata": "sallad",
    "cabbage": "vitkål", "lahana": "vitkål",
    "broccoli": "broccoli", "brokoli": "broccoli",
    "cauliflower": "blomkål", "karnabahar": "blomkål",
    "peas": "ärtor", "bezelye": "ärtor",
    "corn": "majs", "mısır": "majs",
    "green beans": "haricots verts", "taze fasulye": "haricots verts",
    "lemon": "citron", "limon": "citron",
    "lemon juice": "citronjuice", "limon suyu": "citronjuice",
    "apple": "äpple", "elma": "äpple",
    "banana": "banan", "muz": "banan",
    "orange": "apelsin", "portakal": "apelsin",
    "strawberry": "jordgubbar", "çilek": "jordgubbar",
    "pomegranate": "granatäpple", "nar": "granatäpple",
    "pomegranate molasses": "granatäppelsirap", "nar ekşisi": "granatäppelsirap",
    "olive": "oliver", "olives": "oliver", "zeytin": "oliver",
    "avocado": "avokado",
    # herbs & spices
    "parsley": "persilja", "maydanoz": "persilja",
    "dill": "dill", "dereotu": "dill",
    "mint": "mynta", "nane": "mynta",
    "basil": "basilika", "fesleğen": "basilika",
    "oregano": "oregano", "coriander": "koriander", "kişniş": "koriander",
    "thyme": "timjan", "kekik": "timjan",
    "bay leaf": "lagerblad", "defne yaprağı": "lagerblad",
    "cumin": "spiskummin", "kimyon": "spiskummin",
    "paprika powder": "paprikapulver", "toz biber": "paprikapulver",
    "red pepper flakes": "chiliflakes", "pul biber": "chiliflakes",
    "cinnamon": "kanel", "tarçın": "kanel",
    "turmeric": "gurkmeja", "zerdeçal": "gurkmeja",
    "ginger": "ingefära", "zencefil": "ingefära",
    "vanilla": "vanilj", "vanilya": "vanilj",
    "sesame": "sesamfrön", "susam": "sesamfrön",
    # meat & fish
    "ground beef": "nötfärs", "kıyma": "nötfärs", "kiyma": "nötfärs", "dana kıyma": "nötfärs",
    "beef": "nötkött", "dana eti": "nötkött",
    "lamb": "lammkött", "kuzu": "lammkött",
    "chicken": "kycklingfilé", "tavuk": "kycklingfilé",
    "salmon": "laxfilé", "somon": "laxfilé",
    "tuna": "tonfisk", "ton balığı": "tonfisk",
    # pantry tins
    "tomato paste": "tomatpuré", "salça": "tomatpuré", "domates salçası": "tomatpuré",
    # Pepper paste is its own thing — mapping it to tomatpuré prices the wrong jar
    # and hides that Willys may not stock it at all.
    "biber salçası": "ajvar", "pepper paste": "ajvar",
    "chickpeas": "kikärtor", "nohut": "kikärtor",
    "lentils": "röda linser", "mercimek": "röda linser",
    "beans": "vita bönor", "fasulye": "vita bönor",
    "soy sauce": "sojasås",
    "chocolate": "choklad", "çikolata": "choklad",
    "walnuts": "valnötter", "ceviz": "valnötter",
    "almond": "mandel", "badem": "mandel",
    "hazelnut": "hasselnötter", "fındık": "hasselnötter",
    "pistachio": "pistagenötter", "fıstık": "pistagenötter",
    "raisins": "russin", "kuru üzüm": "russin",
}

# Dropped before lookup: they qualify an ingredient, they aren't part of its name.
_QUALIFIERS = {
    "fresh", "dried", "ground", "chopped", "finely", "coarsely", "large", "small",
    "medium", "ripe", "raw", "cooked", "frozen", "canned", "whole", "half", "warm",
    "cold", "hot", "boiling", "lukewarm", "optional", "extra", "plain", "unsalted",
    "taze", "kuru", "ince", "iri", "büyük", "küçük", "sıcak", "soğuk", "ılık", "dolu",
    "ezilmiş", "doğranmış", "kıyılmış", "rendelenmiş", "haşlanmış", "kavrulmuş",
    "kaynamış", "mini",
    "kalın", "yarım", "az", "bir", "biraz",
}

# Free — never worth a lookup or an "unmatched" complaint. Matched on the
# qualifier-stripped name, so "kaynamış su" (boiled water) is free too.
_FREE = {"water", "su", "tap water", "buz", "ice"}


def normalize(item: str) -> str:
    """Ingredient string -> lowercase name, parentheticals and punctuation gone.

    Qualifiers are deliberately KEPT here: several of them are load-bearing parts
    of a name, not decoration. "ground beef" is nötfärs and "beef" is nötkött —
    strip the qualifier first and you confidently price the wrong cut.
    """
    s = (item or "").strip().lower()
    s = re.sub(r"\([^)]*\)", " ", s)          # "(instant)", "(aleppo)"
    s = re.sub(r"[^\w\sÅÄÖåäöÇĞİÖŞÜçğıöşü-]", " ", s)
    return " ".join(s.split()).strip()


def _bare(name: str) -> str:
    """`normalize()`d name with qualifiers dropped — the fallback lookup key."""
    return " ".join(w for w in name.split() if w not in _QUALIFIERS).strip()


def swedish_term(item: str) -> tuple[str, bool]:
    """(search term, was it translated). Untranslated terms are still searched.

    Most specific key first: the full name, then the head phrase ("olive oil,
    extra virgin"), then the same two with qualifiers dropped, and only then the
    trailing noun ("san marzano tomato" -> tomat).
    """
    name = normalize(item)
    if not name:
        return "", False
    # Split the RAW string: normalize() has already turned the separator into a
    # space, so looking for it afterwards never finds one.
    head = normalize(re.split(r"[,;]", item or "")[0])
    for cand in (name, head, _bare(name), _bare(head)):
        if cand and cand in _SV:
            return _SV[cand], True
    # Last word first (English is head-final: "san marzano tomato" -> tomat), then
    # the first (Turkish noun compounds are head-initial: "tavuk göğüs" -> kyckling).
    for cand in (_bare(name), _bare(head)):
        parts = cand.split()
        for w in (parts[-1:] or [""]) + parts[:1]:
            if w and w in _SV:
                return _SV[w], True
    return _bare(name) or name, False


def is_free(item: str) -> bool:
    name = normalize(item)
    return name in _FREE or _bare(name) in _FREE


# ── picking a product ─────────────────────────────────────────────────────────

# Product families that collide with real ingredients and are never one. Cheapest
# -per-kilo alone is a menace here: "nötfärs" matches a tin of CAT FOOD at 38 kr/kg
# and wins on price against actual mince at 160 kr/kg.
_NOT_FOOD = (
    "kattmat", "hundmat", "kattfoder", "hundfoder", "djurgodis",
    "glass", "godis", "chips", "snacks", "barnmat", "välling",
    "örtte", "tepåsar",                     # "pepparmynta örtte" is not fresh mint
    "schampo", "tvål", "tvätt", "rengöring", "servett",
)


def pick(term: str, products: list[Product], *, want_count: bool = False,
         strict: bool = False, qty: dict | None = None) -> Product | None:
    """The product a sensible shopper would put in the basket for `term`.

    Cheapest by jämförpris, but only after two filters, because cheapest-alone is
    reliably wrong:

    * the term must appear in the name as a WHOLE WORD. A substring test lets
      "banan" match "Banana Split Glass" and "vitlök" match a white onion.
    * obvious non-ingredients are dropped outright (`_NOT_FOOD`) — nothing about
      price or relevance distinguishes cat mince from beef mince.

    `want_count` is set when the recipe counts the thing ("2 onions"). Products
    carrying a per-item weight are then preferred, since those are the only ones a
    count can be costed against without billing a whole sack.

    `strict` refuses the relevance fallback and returns None instead. Set it when
    the term was never translated, because then the search string is a guess and
    the store's idea of a near miss is worthless: "ezilmiş sarmısak" went to the
    store verbatim and came back as a 195 kr box of chocolates, which is a far
    worse answer than admitting the ingredient could not be priced.
    """
    term = (term or "").strip().lower()
    if not products:
        return None

    def usable(p: Product) -> bool:
        low = p.name.lower()
        return (not p.out_of_stock and p.compare_price is not None
                and not any(bad in low for bad in _NOT_FOOD))

    pool = [p for p in products if usable(p)]
    if not pool:
        return None

    ranked = [(_rank(term, p.name), p) for p in pool]
    best   = min(r for r, _ in ranked)
    if best == 2:
        # Nothing matched by name. For a translated term that is usually a compound
        # ("creme fraiche" is listed as "Crème Fraiche 34%") and relevance is worth
        # trusting; for a guessed term it is worth nothing at all.
        if strict:
            return None
        candidates = pool[:3]
    else:
        candidates = [p for r, p in ranked if r == best]

    candidates = _sane(candidates)
    if want_count:
        per_piece = [p for p in candidates if p.per_piece_grams or p.compare_unit == "st"]
        candidates = per_piece or candidates

    # Choose by what the BASKET actually costs, not by unit price. Cheapest per
    # kilo is a bulk rule and buys a 5 kg sack of flour to satisfy 300 g; cheapest
    # to-satisfy-this-need buys the smallest bag that covers it — and still buys
    # the sack once the recipe needs 3 kg, because then it genuinely is cheaper.
    if qty is not None:
        def basket_cost(p: Product) -> tuple[float, float]:
            kr, _ = cost_of(qty, p)
            # Tie-break on unit price so equal-cost options prefer better value.
            return (kr if kr is not None else float("inf"), p.compare_price or float("inf"))
        return min(candidates, key=basket_cost)
    return min(candidates, key=lambda p: p.compare_price)


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _rank(term: str, name: str) -> int:
    """0 = name leads with the thing, 1 = mentions it, 2 = doesn't. Lower wins.

    Swedish compounds are head-final, so a word *ending* in the term is a kind of
    that thing (bladpersilja is a parsley) while one merely containing it need not
    be (persiljesås is a sauce). Willys also leads a product name with its noun, so
    position separates the real article from something flavoured with it:
    "Nötfärs 12%" ranks 0, "Burek Nötfärs Fryst" ranks 1, and the mince wins even
    though the pastry is cheaper per kilo.
    """
    head  = term.rsplit(" ", 1)[-1] if term else ""       # "gul lök" -> "lök"
    words = _WORD_RE.findall(name.lower())
    if not head or not words:
        return 2
    if words[0].endswith(head):
        return 0
    return 1 if any(w.endswith(head) for w in words[1:]) else 2


def _sane(candidates: list[Product]) -> list[Product]:
    """Drop jämförpris values too far below the field to be real.

    Genuine alternatives for one ingredient sit within a few times each other;
    an order of magnitude below the median is bad source data, not a bargain.
    Willys lists a jar of dried parsley at 1,00 kr/kg — take it at face value and
    a bunch of parsley costs two öre, which quietly drags the whole basket down.
    """
    if len(candidates) < 3:
        return candidates
    prices = sorted(p.compare_price for p in candidates)
    median = prices[len(prices) // 2]
    kept = [p for p in candidates if p.compare_price >= median / 10.0]
    return kept or candidates


# ── costing ───────────────────────────────────────────────────────────────────

_TSP_ML = 5.0   # 1 tsp ~ 5 ml; parse_qty folds tbsp/cups into tsp already


class Line(NamedTuple):
    """What to buy, in the terms the cart itself uses."""
    units: float          # how many, in `pick_unit` — a count of items, or kilos
    pick_unit: str        # 'pieces' | 'kilogram'
    kr: float | None      # what it adds to the basket
    basis: str            # how that was worked out, for the reader


def _plan_raw(qty: dict, p: Product) -> Line:
    """Decide what to actually buy for this ingredient, and what it costs.

    Price and cart line are the same decision, so they are made in one place: the
    number of packs the estimate charges for is exactly the number the cart has to
    add, and deriving them separately is how the two drift apart.

    Shopping cost, not consumption cost, and the difference is the whole point: a
    recipe using 20 g of flour does not cost 13 öre, it costs one bag of flour,
    because that is what you carry to the till. Packaged goods are therefore
    charged as whole packs — ceil(needed / pack size) — at the shelf price.

    Loose produce is the exception. Nobody sells a sealed pack of onions by the
    unit; you take the three you need and they are weighed. Those are charged for
    the quantity actually required, against the jämförpris.

    Falls back to a single pack whenever the quantity or the pack size is unknown,
    which is the right way to be wrong: you would still have to buy one.
    """
    shelf = p.price
    unit  = p.compare_unit
    pu    = p.pick_unit
    fam   = (qty or {}).get("family") or ""
    lo    = (qty or {}).get("lo")

    def one(why: str) -> Line:
        # A weight-bought product has no "one pack"; half a kilo is the least
        # misleading stand-in, and the basis says it was a guess.
        units = 0.5 if pu == "kilogram" else 1.0
        if shelf is not None:
            return Line(units, pu, shelf if pu == "pieces" else units * shelf, f"1 pack ({why})")
        if p.compare_price is None:
            return Line(units, pu, None, why)
        return Line(units, pu, units * p.compare_price, f"1 pack ({why})")

    if lo is None or not fam:
        return one("no quantity")

    if fam == "spoon":                        # tsp -> ml, then treated as a volume
        lo, fam = lo * _TSP_ML, "volume"

    # ── sold by weight: ask for the weight ────────────────────────────────────
    if pu == "kilogram" and p.compare_price is not None:
        if fam in ("mass", "volume"):
            kg = lo / 1000.0
            return Line(kg, pu, kg * p.compare_price, f"{_g(lo)} @ {p.compare_price:g} kr/{unit or 'kg'}")
        if fam == "count" and p.per_piece_grams:
            kg = lo * p.per_piece_grams / 1000.0
            return Line(kg, pu, kg * p.compare_price, f"{lo:g} × {p.per_piece_grams:g}g by weight")
        return one("weight-sold, no usable quantity")

    # ── loose but bought by the piece: pay for what you take ──────────────────
    if p.is_loose and p.compare_price is not None:
        if fam == "count":
            g = p.per_piece_grams
            if g and unit in ("kg", "l"):
                return Line(lo, pu, lo * g / 1000.0 * p.compare_price, f"loose · {lo:g} × {g:g}g")
            if unit == "st":
                return Line(lo, pu, lo * p.compare_price, f"loose · {lo:g} × {p.compare_price:g} kr/st")
        if fam in ("mass", "volume") and unit in ("kg", "l"):
            # Charged for the weight needed, but the cart still takes a count of
            # items, so ask for as many as cover it.
            g = p.per_piece_grams
            n = max(math.ceil(lo / g - 1e-9), 1) if g else 1.0
            return Line(n, pu, lo / 1000.0 * p.compare_price,
                        f"loose · {_g(lo)} @ {p.compare_price:g} kr/{unit}")

    # ── packaged: whole packs only ────────────────────────────────────────────
    if fam == "count":
        pack = p.pack_count or (1.0 if unit == "st" else None)
        if pack is None:                      # counted, but sold by weight in a pack
            return one("count vs weight")
    else:
        pack = p.pack_grams                   # grams/ml in the sealed pack
    if not pack:
        return one("pack size unknown")
    if shelf is None:
        return one("no shelf price")

    packs = max(math.ceil(lo / pack - 1e-9), 1)   # 1.0000001 packs is one pack
    need  = _g(lo) if fam != "count" else f"{lo:g}"
    size  = _g(pack) if fam != "count" else f"{pack:g}"
    return Line(float(packs), pu, packs * shelf,
                f"{packs} × {size} pack" if packs > 1 else f"1 pack of {size} (need {need})")


def plan(qty: dict, p: Product) -> Line:
    """What to buy, rounded to something the shop can actually sell you.

    Every product states the step it is sold in (`incrementValue`), and a quantity
    off that step is refused as an illegal argument with no hint as to why. Half a
    cucumber is the obvious case — not a thing you can put in a trolley — but the
    same rule covers mince sold in whole ~1 kg packs.

    Rounding up moves the price with it. Buying a whole cucumber to satisfy half of
    one costs a whole cucumber, and the estimate has to say so — otherwise the
    number on screen stops being the number at the till, which is the one thing
    this pair of functions exists to keep true.
    """
    line = _plan_raw(qty, p)
    step  = p.increment if p.increment > 0 else 1.0
    steps = max(math.ceil(line.units / step - 1e-9), 1)
    units = round(steps * step, 3)
    if units == line.units or line.units <= 0:
        return line._replace(units=units)
    kr = None if line.kr is None else line.kr * units / line.units
    return Line(units, line.pick_unit, kr, f"{line.basis} · rounded to {units:g}")


def cost_of(qty: dict, p: Product) -> tuple[float | None, str]:
    """(kr this ingredient adds to the basket, how it was worked out)."""
    line = plan(qty, p)
    return line.kr, line.basis


def _g(v: float) -> str:
    """A quantity in grams/ml rendered the way a label would write it."""
    return f"{v / 1000:g}kg" if v >= 1000 else f"{v:g}g"


# ── the estimate ──────────────────────────────────────────────────────────────

async def estimate(items: list[dict]) -> dict:
    """Price a shopping list at Willys, as a basket you could actually buy.

    `items` is chain-agnostic: [{"item": "ground beef", "qty": {...}}, ...] with
    `qty` as produced by shopping.parse_qty (or absent). Returns

        {"chain", "total", "priced", "items", "rows": [...], "unmatched", "error"}

    A row is a PRODUCT, not an ingredient, and that distinction is what makes the
    total right. "ince bulgur", "kalın bulgur" and "haşlanan bulgur" are three
    lines on the recipe and one bag in the trolley; costing them separately bought
    three bags. So ingredients are resolved to products first, their quantities
    pooled per product, and each product costed once — which is also the more
    useful thing to read, because it is the list you walk the aisles with.

    `total` covers only what could be priced — `unmatched` says what it excludes,
    so the number is never quietly wrong. A store-side failure returns the same
    shape with `error` set rather than raising: an estimate is a nicety, and must
    not be able to fail the request that asked for it.
    """
    unmatched: list[dict] = []
    considered = 0        # ingredients we actually tried to price (water excluded)
    cache = _cache_read()
    dirty = False
    picked: dict[str, dict] = {}      # product code -> {product, names, qty}

    def fail(err: str) -> dict:
        return {"chain": "willys", "total": 0.0, "priced": 0, "items": considered,
                "rows": [], "unmatched": unmatched, "error": err}

    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT_S,
                                 headers={"User-Agent": UA, "Accept": "application/json"}) as client:
        for it in items:
            name = (it.get("item") or "").strip()
            if not name or is_free(name):
                continue

            term, translated = swedish_term(name)
            if not term:
                continue
            considered += 1

            before = len(cache)
            try:
                products = await search(term, client=client, cache=cache)
            except WillysUnavailable as e:
                log.warning("willys: %s", e)
                if dirty:
                    _cache_write(cache)
                return fail(str(e))
            dirty = dirty or len(cache) != before

            qty = it.get("qty") or {}
            p = pick(term, products, want_count=(qty.get("family") == "count"),
                     strict=not translated, qty=qty)
            if p is None:
                why = "no product found" if translated else f"no Swedish term for {name!r}"
                unmatched.append({"item": name, "term": term, "why": why})
                continue

            g = picked.get(p.code)
            if g is None:
                picked[p.code] = {"p": p, "names": [name], "qty": dict(qty)}
                continue
            g["names"].append(name)
            # Pool the need so one pack can cover several lines. Only within one
            # family — 300 g of bulgur plus "some bulgur" is still 300 g, and a
            # quantity we could not read must not silently become zero.
            cur = g["qty"]
            if cur.get("family") and cur.get("family") == qty.get("family") \
                    and cur.get("lo") is not None and qty.get("lo") is not None:
                cur["lo"] += qty["lo"]
                cur["hi"] = (cur.get("hi") or cur["lo"]) + (qty.get("hi") or qty["lo"])

    if dirty:
        _cache_write(cache)

    rows: list[dict] = []
    total = 0.0
    for code, g in picked.items():
        p, qty = g["p"], g["qty"]
        line = plan(qty, p)
        # `units`/`pickUnit` are exactly what the cart call needs, so a basket the
        # user has reviewed on screen is pushed as-is rather than recomputed.
        row = {"items": g["names"], "item": ", ".join(g["names"]),
               "product": p.name, "code": code, "volume": p.display_volume,
               "comparePrice": p.compare_price, "compareUnit": p.compare_unit,
               "units": round(line.units, 3), "pickUnit": line.pick_unit,
               "kr": None if line.kr is None else round(line.kr, 2), "basis": line.basis}
        kr = line.kr
        if kr is None:
            unmatched.append({"item": row["item"], "term": "", "why": basis})
        else:
            total += kr
        rows.append(row)

    return {"chain": "willys", "total": round(total, 2),
            "priced": sum(1 for r in rows if r["kr"] is not None), "items": considered,
            "rows": rows, "unmatched": unmatched, "error": ""}
