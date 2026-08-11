# Family relay

A tiny internet-facing bridge to the family NAS, so the calendar can be used
**away from home** without exposing the NAS. Two channels:

- **Shopping (read-mostly):** the NAS *publishes* a rolling 6-day list (today +
  the next 5 days) up here; the phone reads it and ticks items off at the grocery
  store. Ticks carry forward as the window slides.
- **Calendar (reverse channel):** the phone *queues* "add/delete event" and
  "import recipe" commands into an inbox; the NAS drains it on an outbound poll
  and applies them. The NAS also mirrors members + upcoming events + meals + the
  recipe library here, so the phone app matches the home `mobile.html`.

The NAS/backend (`../backend`) stays firewalled — it only makes **outbound** calls
(like it already does to the Anthropic API). Auth is two long random bearer tokens.

```
 Home LAN (closed)              Cloud relay (this app, HTTPS)          Away
┌──────────────┐ POST /publish   ┌──────────────────────────┐ GET /list  ┌───────┐
│ NAS backend  │ ──────────────► │ shopping snapshot + state │ ◄───────── │ phone │
│              │ GET /inbox      │ calendar mirror + inbox   │ POST /inbox│       │
│  poll loop   │ ◄────── ack ─── │ + serves shop.html (PWA)  │ ◄───────── │       │
└──────────────┘                 └──────────────────────────┘            └───────┘
```

## What it stores

One flat JSON file (`RELAY_DATA`, default `/data/relay.json`) with four sections:
`shopping` (current published week + `bought` state), `calendar` (mirrored members
+ upcoming events + meals), `recipes` (mirrored library — content only, no photos,
no provenance, re-sent only when it changes), and `inbox` (pending commands,
capped at 100). No history, no accounts. Deliberately **never** stored here:
birthdays and recurring items (birth dates are the most sensitive data in the
home stores — they stay off the internet-facing box).

## Auth

| Token | Held by | Can |
|-------|---------|-----|
| `PUBLISH_TOKEN` | the NAS backend | `POST /publish`, `GET /list`, `POST /calendar/publish`, `GET /inbox`, `POST /inbox/ack` |
| `DEVICE_TOKEN`  | all family phones (read/shop scope) | `GET /list`, `POST /state`, `GET /calendar`, `GET /recipes`, `GET /app-config` |
| `DEVICE_WRITE_TOKEN` | phones allowed to add calendar entries | `POST /inbox` |

`DEVICE_WRITE_TOKEN` is a **separate write scope** so a leaked read/shop token
can't inject calendar entries. If unset, it falls back to `DEVICE_TOKEN`
(single-token mode). Provision a phone's write ability by appending `&w=<code>`
to the setup link (see below). Tokens are accepted in the `Authorization`
header **only** — the provisioning link carries them in the URL *fragment*
(`#t=…`), which never reaches the server or its logs.

## Hardening built in

- **Least-privilege tokens** (read/shop vs write vs publish), constant-time compared.
- **Strict CSP** — the phone page has no inline script (logic is in `/app.js`), so
  `script-src 'self'` (no `unsafe-inline`) — plus `X-Content-Type-Options`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, HSTS, `Permissions-Policy`.
- **Per-IP rate limiting** (`RATE_LIMIT`/`RATE_WINDOW`, default 120/60s; `/healthz` exempt)
  and a **request body cap** (`MAX_BODY`, default 1 MB — sized for the recipe-library mirror).
- **Runs as a non-root user** and is **read-only-rootfs compatible** (writes only the
  data volume).
- **Server-side validation** of every queued command happens on the NAS
  (`_sanitize_event`), the inbox is capped, and a **cost fuse** on the NAS
  drain runs at most 5 paid-AI commands (recipe import/photo) per cycle —
  a leaked write token can't burn API credit faster than that.
- Escaping: all home UIs HTML-escape event/meal text, so a queued entry can't XSS
  the dashboard. Data is low-sensitivity (grocery items + queued entries only).

Sent as `Authorization: Bearer <token>` (the phone page may also pass `?t=`).
Compared in constant time. Generate them with:

```sh
openssl rand -hex 32   # PUBLISH_TOKEN
openssl rand -hex 32   # DEVICE_TOKEN
```

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/publish` | publish | NAS pushes the rolling window `{week, start, days, have, extras}`. `bought` ticks carry forward as the window slides and are pruned only when their item rolls off. |
| GET  | `/list` | device or publish | Current published list + check-off state. |
| POST | `/state` | device | Update `{have, bought}`. |
| POST | `/calendar/publish` | publish | NAS mirrors `{members, events, eventColors, meals, recipesSig}` (+ `recipes` when the library changed); responds with the stored `recipesSig` so the NAS knows when to re-send. |
| GET  | `/calendar` | device or publish | Mirrored members, upcoming events, the week's `meals`, and the current `recipesSig`. |
| GET  | `/recipes` | device or publish | Mirrored recipe library (index + records) for the Browse tab; the phone refetches only when `recipesSig` moves. |
| GET  | `/app-config` | device or publish | Phone-app config: the home backend URL for the AI content tab. |
| POST | `/inbox` | device **write** | Queue a command; returns the assigned `id`. Types: `event`, `event_delete`, `recipe_import` (`{url｜text}`), `recipe_photo` (`{image, media}`, downscaled base64 JPEG), `shopping_extra` (`{week, item, action?}`, a manual store-trip item). |
| GET  | `/inbox` | publish | Pending add-commands, for the NAS to drain. |
| POST | `/inbox/ack` | publish | Drop applied commands: `{ids: [...]}`. |
| GET  | `/` | — | Serves `shop.html` (the phone page). |
| GET  | `/manifest.webmanifest`, `/sw.js`, `/icon-*.png`, `/apple-touch-icon.png` | — | PWA assets (installable app + offline shell). |
| GET  | `/healthz` | — | Liveness. |

The phone page is an installable **PWA**: a web manifest + icons give it a
home-screen icon and full-screen launch, and `sw.js` caches the app shell so it
loads instantly and offline. The list data itself is not cached by the service
worker — the page keeps its own `localStorage` copy and syncs when online.
Regenerate the icons with `make_icons.py` if you want to restyle them.

> ⚠️ **Editing the app shell? Bump the cache.** `sw.js` serves the shell
> (`/`, `app.js`, icons) **cache-first**, and re-fetches it only when `sw.js`'s
> own bytes change. So any change to `shop.html` or `app.js` **must** be paired
> with bumping `const CACHE = 'shop-shell-vN'` in `sw.js` — otherwise returning
> phones keep the old shell even after `fly deploy`, and you get a confusing
> split where NAS-side data changes show up but UI changes don't. (This is how
> the F9 add-extra input first went missing on already-installed phones.)

## Run locally

```sh
PUBLISH_TOKEN=dev-pub DEVICE_TOKEN=dev-dev DEVICE_WRITE_TOKEN=dev-wr RELAY_DATA=./relay.json \
  uvicorn main:app --port 9000
# phone page (shop only):        http://localhost:9000/#t=dev-dev
# phone page (shop + add cal.):  http://localhost:9000/#t=dev-dev&w=dev-wr
```

## Deploy (Fly.io)

> For a full walkthrough (CLI install, tokens, volume, phone provisioning,
> troubleshooting, Render/Railway), see **[SETUP.md](SETUP.md)**. Quick version:

```sh
fly apps create family-shopping-relay          # or edit app= in fly.toml
fly volumes create relay_data --size 1 -r arn  # persistent store for relay.json
fly secrets set PUBLISH_TOKEN=$(openssl rand -hex 32) \
                DEVICE_TOKEN=$(openssl rand -hex 32)
fly deploy
fly secrets list                               # (values are write-only; note them when you set them)
```

Render / Railway work the same way: build the Dockerfile, attach a persistent
disk mounted where `RELAY_DATA` points, and set `PUBLISH_TOKEN` / `DEVICE_TOKEN`.

## Wire up the NAS

On the calendar backend set:

```
SHOP_RELAY_URL=https://family-shopping-relay.fly.dev
SHOP_RELAY_PUBLISH_TOKEN=<the PUBLISH_TOKEN>
SHOP_RELAY_POLL_SECONDS=90     # optional; how often the NAS mirrors + drains (default 90)
```

`SHOP_RELAY_POLL_SECONDS` powers the reverse channel: when the relay is
configured, the NAS runs a background loop that every ~90s mirrors the calendar
up and drains any queued add-commands. So an event added from the phone lands at
home within a poll interval.

Then provision each phone once by opening
`https://family-shopping-relay.fly.dev/#t=<the DEVICE_TOKEN>` — the code is
saved locally and stripped from the URL. Publishing the shopping list happens
automatically when a meal plan or the at-home list changes, or via the **Publish
to phone** button in the app's Shopping tab.

## What the phone can do from anywhere

The phone page (`shop.html`) mirrors the home `mobile.html` tab-for-tab — all
reachable off the home LAN, with the NAS staying closed (reads come from the
mirror, writes are queued and applied by the NAS on its next poll):

- **📅 Events** — add an event (person picker + icons from the mirror), see
  upcoming events, ✕ delete one. Queued writes.
- **🍽 Meals** — *read-only* view of the week's dinners and today's/tomorrow's
  recipe (ingredients + steps), from the mirrored `calendar.meals`.
- **📖 Recipes** — **Scan a recipe / Upload a photo** (downscaled client-side,
  queued; the NAS AI-reads it in its original language and saves it flagged
  *Review needed* in the home Recipe editor — duplicates land under Pending
  imports) and **Import a recipe** (paste a URL or text → queued; the NAS
  fetches, AI-extracts, and saves it — review later in the home app).
- **🔍 Browse** — search/filter the whole mirrored recipe library (name,
  ingredient, tag, course, cuisine) and read any recipe.
- **🛒 Shopping** — the rolling 6-day list (today + next 5 days) grouped by aisle
  with day-filter chips and cumulative amounts; tick items (read + `POST /state`),
  and add manual extras (F9, queued via `POST /inbox`).

Live-only features stay in the home app: AI meal planning and the *reviewed*
photo-scan flow need the live backend; the relay's scan is fire-and-forget
(save now, review at home). Writes need the **write
scope** (`DEVICE_WRITE_TOKEN`); the everyday `DEVICE_TOKEN` only reads and
ticks. The NAS strictly validates and sanitizes every queued command, and
applies it within one poll interval (~90s).
