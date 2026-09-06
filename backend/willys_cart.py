"""Willys cart push — the WRITE half, deliberately kept apart from willys.py.

`willys.py` is anonymous and read-only and says so in its own docstring; this
module signs in as you and changes something real, so it lives on its own with
its own failure handling. The split is the safety property: a break in pricing
can only blank an estimate, and nothing in here can run unless a person asked
for it.

    push(rows)  ->  add a reviewed basket to the Willys cart

The request contract was established by capture (see the session notes) rather
than guessed, and three details in it are load-bearing:

  * `pickUnit` is required and takes the WORDS `pieces` or `kilogram` — not the
    `ST`/`KG` codes the product data carries. Omit it and the API answers 400
    `pickUnit: {basket.error.pickUnit.notNull}`; send the code instead of the
    word and it answers 400 `{error.illegal.argument}`.
  * `qty` means different things per unit: a count of items for `pieces`, a
    weight in KILOS for `kilogram`, where 0.5 is half a kilo. `willys.plan()`
    decides both, so the number charged on the estimate is the number added here.
  * the CSRF token is bound to the servlet session (`JSESSIONID`) and the
    load-balancer node (`ROUTE`). Fetch the token on one connection and post on
    another and it answers 401 `csrf.badormissing` — which is why everything runs
    on a single `httpx.AsyncClient` whose cookie jar carries all of it.

The trap worth knowing about: Willys gives an *unauthenticated* session its own
perfectly working cart. Push with the wrong cookies and every call succeeds, 200
after 200, filling a cart no human will ever open. `anonymousOrder` in the
response is the only thing that tells them apart, so it is checked on every push
and treated as a hard failure.

Auth is a session imported from a browser — never a password. You sign in at
willys.se, copy the `Cookie` request header, and put it in

    data/willys_session.json   {"cookie": "JSESSIONID=…; ROUTE=…; …"}

git-ignored, and the only secret involved. When it lapses the module says so and
stops; it cannot log itself back in, which is the point.
"""

from __future__ import annotations

import json
import logging

import httpx

from storage import DATA
from willys import BASE, TIMEOUT_S, UA

log = logging.getLogger(__name__)

SESSION_FILE = DATA / "willys_session.json"
CSRF_PATH    = "/axfood/rest/v1/csrf-token"
ADD_PATH     = "/axfood/rest/v1/cart/addProduct"
CART_PATH    = "/axfood/rest/v1/cart"
CUSTOMER_PATH = "/axfood/rest/v1/customer"


class CartUnavailable(Exception):
    """The cart could not be reached, or the imported session is no longer good."""


def session_cookie() -> str:
    """The imported browser session, or '' when none has been set up."""
    try:
        return (json.loads(SESSION_FILE.read_text("utf-8")).get("cookie") or "").strip()
    except Exception:
        return ""


def configured() -> bool:
    return bool(session_cookie())


def _client(cookie: str) -> httpx.AsyncClient:
    # One client for the whole push: the jar picks up JSESSIONID/ROUTE from the
    # token call and carries them into the POSTs, which is the entire fix for
    # `csrf.badormissing`. Doing this by hand is what makes it hard in curl.
    return httpx.AsyncClient(
        base_url=BASE, timeout=TIMEOUT_S, follow_redirects=False,
        headers={"User-Agent": UA, "Accept": "application/json", "Cookie": cookie},
    )


async def _token(client: httpx.AsyncClient) -> str:
    """The CSRF token. The endpoint answers with a bare JSON string, not an object."""
    r = await client.get(CSRF_PATH)
    r.raise_for_status()
    tok = r.json()
    if not isinstance(tok, str) or not tok:
        raise CartUnavailable(f"csrf-token returned {type(tok).__name__}, expected a string")
    return tok


async def whoami(client: httpx.AsyncClient) -> str:
    """The signed-in customer id, or 'anonymous'. The session check before writing."""
    r = await client.get(CUSTOMER_PATH)
    r.raise_for_status()
    return str((r.json() or {}).get("uid") or "anonymous")


async def _add(client: httpx.AsyncClient, tok: str, code: str, units: float,
               pick_unit: str) -> tuple[dict, str]:
    """Add one product. Returns (cart, token) — the token may have been renewed.

    Parameters ride in the query string and there is no body, so an explicit
    `Content-Length: 0` is required. The 401 retry is the storefront's own
    recovery path, copied rather than invented: its client refetches the token and
    replays the request exactly once.
    """
    qty = f"{units:g}"
    params = {"productCodePost": code, "qty": qty, "pickUnit": pick_unit}
    headers = {"X-CSRF-Token": tok, "Content-Length": "0"}

    r = await client.post(ADD_PATH, params=params, headers=headers)
    if r.status_code == 401 and "csrf" in r.text.lower():
        tok = await _token(client)
        r = await client.post(ADD_PATH, params=params,
                              headers={"X-CSRF-Token": tok, "Content-Length": "0"})

    if r.status_code != 200:
        # These errors name the offending field, so pass them through verbatim
        # rather than flattening them into "add failed".
        raise CartUnavailable(f"{code}: HTTP {r.status_code} {r.text[:200]}")
    return r.json() or {}, tok


async def push(rows: list[dict]) -> dict:
    """Add a reviewed basket to the Willys cart.

    `rows` are `willys.estimate()` rows — each needs `code`, `units` and
    `pickUnit`. Rows without a code (nothing matched) are skipped, not guessed at.

    Adds one product at a time. `addProducts` would do the lot in one request, but
    a per-product call means a single rejected line reports which one it was and
    leaves the rest of the basket intact.
    """
    cookie = session_cookie()
    if not cookie:
        raise CartUnavailable(f"No Willys session. Sign in at willys.se, copy the "
                              f"Cookie request header, and write it to {SESSION_FILE}")

    lines = [r for r in rows if (r.get("code") and r.get("units"))]
    if not lines:
        return {"added": 0, "products": [], "total": "", "customer": "", "skipped": len(rows)}

    async with _client(cookie) as client:
        try:
            who = await whoami(client)
        except httpx.HTTPError as e:
            raise CartUnavailable(f"cannot reach Willys: {e}") from e
        if who == "anonymous":
            raise CartUnavailable(
                "The imported Willys session is not signed in — anything pushed would "
                "land in an anonymous cart you will never see. Sign in at willys.se "
                f"and re-copy the Cookie header into {SESSION_FILE}")

        tok  = await _token(client)
        cart: dict = {}
        added = 0
        for row in lines:
            cart, tok = await _add(client, tok, row["code"], float(row["units"]),
                                   row.get("pickUnit") or "pieces")
            added += 1
            # Checked every time, not once at the end: a session that lapses
            # mid-push would silently divert the remainder into a stray cart.
            if cart.get("anonymousOrder"):
                raise CartUnavailable(
                    f"Willys switched to an anonymous cart after {added} product(s) — "
                    "the session lapsed mid-push. Nothing more was added.")

    log.info("willys cart: added %d products for %s", added, who)
    return {
        "added":    added,
        "skipped":  len(rows) - len(lines),
        "customer": who,
        "total":    cart.get("totalPrice") or "",
        "items":    cart.get("totalItems") or 0,
        "products": [{"name": p.get("name"), "qty": p.get("quantity"),
                      "price": p.get("totalPrice")}
                     for p in (cart.get("products") or [])],
    }
