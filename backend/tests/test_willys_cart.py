"""Willys cart push: session handling, the request contract, and the traps.

Offline — every HTTP call is served by a fake transport. The cases are the ones
live capture actually produced, including the two 400s that named their own
missing field and the anonymous cart that succeeds while doing nothing useful.
"""
import json

import httpx
import pytest

import willys_cart
from willys import Product
from willys_cart import CartUnavailable


def _row(code="101822876_ST", units=1, pick="pieces"):
    return {"code": code, "units": units, "pickUnit": pick, "item": "bulgur"}


CART_OK = {"anonymousOrder": False, "totalItems": 1, "totalPrice": "17,50 kr",
           "products": [{"name": "Bulgur", "quantity": 1, "totalPrice": "17,50 kr"}]}


class Fake:
    """Stands in for Willys. Records requests so the contract can be asserted."""

    def __init__(self, *, uid="testuser1", cart=None, add_status=200, add_body=None,
                 csrf_401_once=False):
        self.uid, self.cart = uid, cart or CART_OK
        self.add_status, self.add_body = add_status, add_body
        self.csrf_401_once = csrf_401_once
        self.adds, self.tokens = [], 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/customer"):
            return httpx.Response(200, json={"uid": self.uid})
        if path.endswith("/csrf-token"):
            self.tokens += 1
            return httpx.Response(200, json=f"token-{self.tokens}")
        if path.endswith("/cart/addProduct"):
            self.adds.append(request)
            if self.csrf_401_once and len(self.adds) == 1:
                return httpx.Response(401, json={"error": "csrf.badormissing"})
            if self.add_status != 200:
                return httpx.Response(self.add_status, json=self.add_body or {})
            return httpx.Response(200, json=self.cart)
        return httpx.Response(404)


@pytest.fixture()
def wire(monkeypatch, tmp_path):
    """Point the module at a temp session file and a fake Willys."""
    monkeypatch.setattr(willys_cart, "SESSION_FILE", tmp_path / "willys_session.json")

    def install(fake: Fake, cookie="JSESSIONID=abc; ROUTE=.node1"):
        if cookie is not None:
            willys_cart.SESSION_FILE.write_text(json.dumps({"cookie": cookie}))
        real = willys_cart._client
        monkeypatch.setattr(willys_cart, "_client", lambda c: httpx.AsyncClient(
            base_url="https://www.willys.se", transport=httpx.MockTransport(fake.handler),
            headers={"Cookie": c}))
        return fake
    return install


# ── session ───────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_session_is_a_clear_instruction_not_a_crash(wire, anyio_backend):
    wire(Fake(), cookie=None)
    with pytest.raises(CartUnavailable) as e:
        await willys_cart.push([_row()])
    assert "Sign in at willys.se" in str(e.value)


@pytest.mark.anyio
async def test_an_anonymous_session_refuses_to_push_anything(wire, anyio_backend):
    """Willys gives a signed-out session a WORKING cart, so every call would
    return 200 while filling a trolley nobody will ever open."""
    fake = wire(Fake(uid="anonymous"))
    with pytest.raises(CartUnavailable) as e:
        await willys_cart.push([_row()])
    assert "not signed in" in str(e.value)
    assert fake.adds == []                       # nothing was sent


@pytest.mark.anyio
async def test_a_session_that_lapses_mid_push_stops_immediately(wire, anyio_backend):
    """The check is per product, not once up front: the remainder would otherwise
    divert into a stray cart while still reporting success."""
    fake = wire(Fake(cart={**CART_OK, "anonymousOrder": True}))
    with pytest.raises(CartUnavailable) as e:
        await willys_cart.push([_row(), _row("2_ST"), _row("3_ST")])
    assert "lapsed mid-push" in str(e.value)
    assert len(fake.adds) == 1                   # stopped at the first, not all three


# ── the request contract ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_sends_the_three_parameters_the_api_demands(wire, anyio_backend):
    fake = wire(Fake())
    await willys_cart.push([_row(units=2)])
    q = fake.adds[0].url.params
    assert q["productCodePost"] == "101822876_ST"
    assert q["qty"] == "2"
    assert q["pickUnit"] == "pieces"


@pytest.mark.anyio
async def test_a_weight_bought_product_asks_for_kilos(wire, anyio_backend):
    """`qty` is a weight for kilogram items — 0.5 is half a kilo, not half a pack."""
    fake = wire(Fake())
    await willys_cart.push([_row(units=0.5, pick="kilogram")])
    q = fake.adds[0].url.params
    assert q["qty"] == "0.5" and q["pickUnit"] == "kilogram"


@pytest.mark.anyio
async def test_sends_the_token_and_a_zero_content_length(wire, anyio_backend):
    """There is no body — the parameters ride in the query string — and the API
    rejects the POST outright without an explicit Content-Length."""
    fake = wire(Fake())
    await willys_cart.push([_row()])
    h = fake.adds[0].headers
    assert h["x-csrf-token"] == "token-1"
    assert h["content-length"] == "0"


@pytest.mark.anyio
async def test_a_stale_token_is_refetched_and_the_add_replayed(wire, anyio_backend):
    """The storefront's own recovery rule, copied rather than invented."""
    fake = wire(Fake(csrf_401_once=True))
    out = await willys_cart.push([_row()])
    assert len(fake.adds) == 2                              # first 401, then replayed
    assert fake.adds[1].headers["x-csrf-token"] == "token-2"
    assert out["added"] == 1


@pytest.mark.anyio
async def test_one_token_serves_a_whole_basket(wire, anyio_backend):
    fake = wire(Fake())
    await willys_cart.push([_row("a_ST"), _row("b_ST"), _row("c_ST")])
    assert fake.tokens == 1 and len(fake.adds) == 3


# ── failures ──────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_rejected_line_names_the_field_the_api_complained_about(wire, anyio_backend):
    """400s here are self-describing — 'pickUnit: {basket.error.pickUnit.notNull}'
    is the whole diagnosis, so it must not be flattened into 'add failed'."""
    wire(Fake(add_status=400,
              add_body={"errors": ["pickUnit: {basket.error.pickUnit.notNull}"]}))
    with pytest.raises(CartUnavailable) as e:
        await willys_cart.push([_row()])
    assert "101822876_ST" in str(e.value) and "pickUnit" in str(e.value)


@pytest.mark.anyio
async def test_rows_that_never_matched_a_product_are_skipped_not_guessed(wire, anyio_backend):
    fake = wire(Fake())
    out = await willys_cart.push([_row(), {"item": "mini Yedikule Marulu"}])
    assert len(fake.adds) == 1 and out["added"] == 1 and out["skipped"] == 1


@pytest.mark.anyio
async def test_an_empty_basket_touches_nothing(wire, anyio_backend):
    fake = wire(Fake())
    out = await willys_cart.push([{"item": "unmatched"}])
    assert out["added"] == 0 and fake.adds == [] and fake.tokens == 0


@pytest.mark.anyio
async def test_reports_the_cart_it_ended_up_with(wire, anyio_backend):
    wire(Fake())
    out = await willys_cart.push([_row()])
    assert out["customer"] == "testuser1"
    assert out["total"] == "17,50 kr"
    assert out["products"] == [{"name": "Bulgur", "qty": 1, "price": "17,50 kr"}]


# ── pick_unit comes from the product, not the price ───────────────────────────

def test_pick_unit_follows_how_the_thing_is_bought():
    """A banana is priced per kilo and bought by the piece; only basket type says so."""
    def p(bt):
        return Product(code="x", name="n", manufacturer="", price=1.0, compare_price=1.0,
                       compare_unit="kg", display_volume="ca: 180g", out_of_stock=False,
                       basket_type=bt)
    assert p("ST").pick_unit == "pieces"
    assert p("KG").pick_unit == "kilogram"
