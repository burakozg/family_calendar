# Architecture

## Overview

The Family Calendar is a three-tier system with a deliberately simple core:
a FastAPI backend backed by flat JSON files, a set of static HTML clients, and
a battery-powered e-ink device. There is no database and no build step.

```
                         ┌──────────────────────────────────────────┐
                         │              FastAPI backend               │
   Web clients           │                                            │
  ┌───────────┐  POST    │  Routes ──► read/write JSON ──► rebuild     │
  │ admin     │─────────►│                     display cache          │
  │ mobile    │          │                          │                 │
  │ display   │◄─── SSE ─┤  broadcast("update") ◄────┘                 │
  └───────────┘          │                                            │
                         │  /display-data ──► cache/display.json      │
  ┌───────────┐  poll    │                                            │
  │ Inky Frame│◄────────►│  /meals/suggest ──► Anthropic API          │
  │ Pico 2 W  │          │                                            │
  └───────────┘          └──────────────────────────────────────────┘
                                          │
                                   data/*.json (on disk)
```

### Key design decision: the pre-rendered display cache

The Inky Frame runs MicroPython on a microcontroller and must do **zero
computation** at fetch time to save battery and stay simple. So the backend
does all the work up front:

- Every time data changes (any POST/PATCH/DELETE), the backend calls
  `build_display_cache()`, which expands birthdays, recurring items, events, and
  (when `settings.display.showHolidays` is on) Swedish red days into a concrete
  per-day map, builds a **rolling four-week grid** — 28 cells, always starting on
  the Monday of the current week — flags holiday cells (red day number) plus a
  `Holiday` legend entry, attaches the current week's meal plan (plus
  today's/tomorrow's recipe detail) and a color legend, then writes it to
  `data/cache/display.json`.
- Because the window is anchored on today rather than on a calendar month, it
  spans two months most weeks. There is no off-month concept and no shading:
  the 1st of a month carries a `month_short` so it renders as "1 Sep", and the
  payload's `title` names the range ("Aug - Sep 2026").
- The device just GETs `/display-data`, receives a ready-to-draw payload, and
  renders it. Shifted windows (`week_offset != 0`) are computed on demand and
  not cached. The cache is discarded whenever its `today` is no longer today,
  which is also what re-anchors the window as days pass.

This "materialized view" pattern is the heart of the system.

## Backend (`backend/`)

A FastAPI app split into focused modules (dependencies flow strictly
downward — no import cycles):

| Module | Role |
|--------|------|
| `config.py` | Loads `.env` (import side effect), AI model + key. |
| `fsatomic.py` | Atomic file writes (temp + fsync + rename). |
| `activity_log.py` | Structured JSONL log, `read_logs`, `parse_ai_json`. |
| `storage.py` | Paths, `DEFAULTS`, the global write lock, stable ids, input sanitizers, recipe file store, aisle taxonomy. |
| `ai.py` | Provider-agnostic LLM layer: model registry, per-provider request/response shaping, the single `complete()` / `complete_or_none()` choke point. |
| `ai_usage.py` | Append-only token ledger (`data/ai_usage.jsonl`) + the per-day/per-model rollup behind `/ai/usage`. |
| `bus.py` | SSE fan-out (`broadcast`). |
| `display_cache.py` | Recurrence engine + the pre-rendered device payload. |
| `swedish_holidays.py` | Local Swedish red-day computation (fixed + Easter + floating Saturdays); consumed by `display_cache.py`. |
| `calendar_store.py` | Shared add/delete mutation helpers (routes, relay, mailsync). |
| `relay_client.py` | Cloud-relay mirror, command-inbox drain, sync loop. |
| `shopping.py` | Shopping payload, AI aisle tagging, relay publish + routes. |
| `meals.py` | Weekly plan + two-step AI planner + routes. |
| `recipes.py` | Recipe CRUD + AI extraction/import pipelines + routes. |
| `mailsync.py` | mailbox.org mail/calendar sync (see "Mail/calendar sync" below). |
| `main.py` | App assembly: middleware (Host allowlist, body cap, X-Who), settings/events routes, logs/display/SSE, backup loop, startup, static mounts. |

Domain routes live on `APIRouter`s included by `main.py`. Responsibilities:

### Storage
Flat JSON files under `/data` (mounted volume):

| File | Contents |
|------|----------|
| `settings.json` | Family name, members (id/label/bg/text colors), display prefs, event colors, meal prompt. |
| `events.json` | `events` (one-off: `date`, optional `endDate` for multi-day ranges, optional `time` "HH:MM", `who`, `icon`, `label`), `birthdays` (month/day + optional birth `year`, shown as the age they turn; recur yearly), `recurring` (start date + either legacy `step` in days or v2 `freq: daily\|weekly` with `interval`/`byday`/`until`/`exdates`; optional `time`; an optional `who` colors the icon by member). **Every item carries a stable random `id`** (lazily migrated on first read) — deletes go through `/…/by-id/{id}` routes; frontends must preserve `id` on whole-document saves. |
| `meals.json` | `plan` keyed by ISO week (`YYYY-WW`) — the assembled weekly dinners. |
| `shopping.json` | Per-week `{have, bought, extras}` state for the shopping list — check-off state plus manual extra items (F9); the recipe-derived item lists themselves are computed from the plan on the fly. |
| `recipes/` | Recipe library (the source of meal options): one `{id}.json` per recipe, an `index.json` summary, `photos/` for scanned images, and `pending/` for import drafts awaiting a duplicate-merge review (F8c). |
| `cache/display.json` | Pre-rendered device payload (auto-regenerated, see above). |
| `logs.jsonl` | Activity log, one JSON object per line (see "Activity log" below). |
| `relay_applied.json` | Ids of relay inbox commands already applied (at-least-once dedupe). |
| `backups/` | Daily zips of the stores + recipe records (last 14 kept). |

The data root defaults to `/data` and is configurable via the `DATA_DIR` env
var (used by tests/local runs). `read(path, key)` lazily seeds a file from the
`DEFAULTS` dict on first access, so the app boots with sample data if the data
dir is empty; a corrupt store file is logged (`system/data.corrupt`) and the
error propagates — it is never silently reset. All writes go through
`_atomic_write_text()` (temp file + fsync + `os.replace`), so a crash or power
loss cannot leave a torn file. A single global `asyncio.Lock` (`_write_lock`)
serializes every read-modify-write on the stores (routes, relay drain,
AI-apply steps, backups, cache rebuilds). The rule: only top-level route/loop
bodies and the shared `_add_*`/`_delete_*` helpers acquire it, nothing called
under it may acquire it again (it is not reentrant), AI/network calls stay
outside it, and after any `await` the store is re-read inside the lock before
applying.

A background loop zips the JSON stores + recipe records into
`backups/YYYY-MM-DD.zip` once a day (keeps 14; photos/cache/logs excluded).
Oversized request bodies are rejected early by a `MAX_BODY` middleware
(default 64 MB — generous because photo/zip uploads arrive base64-in-JSON).

### Recipe library
The recipe store (`/recipes` routes) is the single source of recipes and the
pool the meal planner draws from. Each recipe is its own file; a derived
`index.json` holds a summary row per recipe (`_recipe_index_entry`) so listing
and the recipe viewer can filter/search without opening every file — it carries
`course`, `dietary`, `servings`, `total_time_min`, lowercased `ingredients`
(item names, for text search), `source_value`, plus the usual name/cuisine/
tags/rating. `_upsert_index()` keeps the index in sync and sorted on every
create/update; `rebuild_recipe_index()` regenerates it from the files
(`POST /recipes/reindex`, and once on startup when `recipe_index_needs_rebuild()`
detects a pre-extension index). Each recipe also carries a **`course`** (the
kind of dish — a `RECIPE_COURSES` enum: main/side/soup/salad/dessert/breakfast/
baking/snack/drink/other, a separate axis from `meal_type`'s occasion),
validated on create/update/AI-generate (invalid → `""`, shown as "Other"). The
admin library groups by course and `POST /recipes/classify-courses` backfills
missing ones with a single batched AI call (keyword fallback when the AI is
unavailable). Recipes can be entered by hand (admin UI),
scanned from a photo via `POST /recipes/extract` (AI vision, which writes the
image under `recipes/photos/` and links it in the recipe's `photos[]`), or
imported from a **URL or pasted text** via `POST /recipes/extract-text` (mobile
"Import recipe"). The URL fetch is **SSRF-guarded** (`_host_is_public` rejects
loopback/private/link-local/metadata hosts, http(s) only, bounded redirects each
re-validated, size cap); the page is reduced to recipe text (JSON-LD kept, tags
stripped) before the AI extracts the same per-field-provenance draft the photo
flow uses. All three paths land in the same review-and-save UI, tagged with the
recipe's `source.type` (`photo` / `url` / `text`).

Beyond the review flows there are three save-directly paths: `POST
/recipes/import-link` imports **one recipe from a URL** (recipe site or
Instagram post) or pasted text and saves it immediately (the editor is the
review step); `POST /recipes/import-notion`
**bulk-imports a Notion "Markdown & CSV" export** (.zip, base64, capped at 80
pages, a few concurrent AI calls) — index-like pages are skipped and link-only
notes are followed to their target page; and `POST /recipes/ai-generate`
creates recipes from scratch (marked `source: ai`), used to seed the planner's
fallback pool. `POST /recipes/normalize-units` batch-standardizes existing
recipes to g/kg, °C, and cups/tbsp/tsp.

**Language & duplicate handling (F8).** Extraction keeps content fields
(`name`, `description`, `ingredients[].item/.notes`, `steps`, `notes`,
`variations`) in the source's **original language**; only taxonomy (`cuisine`,
`tags`, `meal_type`, `dietary`, `course`, aisle `category`) is English. Every
import runs `find_similar(name, source, ingredients)` — an offline (no-AI)
matcher over the index: same source identity (Notion page title / URL, score
1.0) → folded-name ratio ≥ 0.75 → name ≥ 0.6 **and** ingredient Jaccard ≥ 0.5.
A match doesn't duplicate: the draft is written to `data/recipes/pending/`
(`GET/POST /recipes/pending`, included in backups/export) and surfaced in the
admin **Pending imports** banner. `POST /recipes/pending/{pid}/resolve` either
`merge`s chosen fields into the match (content defaults to take-new to restore
original language; `id`/`log`/`rating` always preserved, `photos` unioned),
saves it as a new recipe (`create`), or discards it. The extract routes also
return `matches` so review UIs can warn before saving. This is what lets the
old force-translated Notion recipes be re-imported and merged back to their
Turkish/Swedish originals without creating duplicates.

### Activity log
`log_event(category, action, message, level, who, detail)` appends structured
JSON lines to `data/logs.jsonl` (bounded to the most recent ~3000 entries; the
helper never raises). Categories are the fixed `LOG_CATEGORIES` list (`ai`,
`import`, `connectivity`, `cloud`, `data`, `system`), levels `info`/`warn`/
`error`. A middleware captures the `X-Who` request header into a contextvar so
entries record who initiated the action (admin/mobile send it). The admin
**Logs** tab reads `GET /logs` (filters: category/level/who/text/since-date,
newest first) and `GET /logs/meta` (filter options — the categories list comes
from the backend, so adding a category needs no frontend change);
`DELETE /logs` clears the file. Coverage spans AI calls, imports,
connectivity, cloud-relay activity, **data mutations** (events/birthdays/
recurring/settings/meals/recipes CRUD), skipped-malformed-entry warnings from
the cache builder, and lifecycle (start, backups, manual rebuilds). The relay
service (`shopping-relay/`) has no logging of its own yet — a known gap, not
an oversight in this list.

### Live updates (SSE)
`/stream` is a Server-Sent Events endpoint. Each connected web client gets an
`asyncio.Queue` in the `_subscribers` list. After any mutation the route calls
`broadcast("update", {"section": ...})`, which fans the message out to every
queue; clients react by re-fetching the affected section. A 25-second keepalive
comment keeps connections alive, and disconnected/full queues are pruned. The
Inky Frame does **not** use SSE — it polls.

### AI meal planning (two steps)
Meal planning is a two-step conversation with Claude (the app-wide `AI_MODEL`,
currently `claude-sonnet-4-6`), driven
by three prompts stored in `settings.mealPlanner` (editable in the admin
Settings page, with sensible built-in defaults — see `DEFAULT_*` constants and
`_meal_prompts()`):

- **`initialPrompt`** — the family's standing weekly instruction (what to aim for
  every week).
- **`systemGenerate`** — the system prompt for step 1.
- **`systemRefine`** — the system prompt for step 2.
- **`recentWeeks`** / **`staleWeeks`** — planner history windows (defaults 2 / 6):
  how many weeks back to avoid repeats, and how long a dish must be unseen before
  it's offered back for variety.

**Step 1 — `POST /meals/plan/generate`.** Builds the user message from the
standing prompt, the **recipe library** (`index.json`) as the option set, the
recipes served in the **recent weeks** (derived server-side from the saved plan
by `_recent_from_history` — the client sends only the target week's `weekStart`,
so repeats are avoided without trusting a client `recent` list), a **"not cooked
in a while"** variety hint (`_long_time_no_cook` — library dishes unseen for
`staleWeeks`+), and the week's **day-by-day events**. The model returns 7
dinners (Mon→Sun) as `{id, name, notes}` — quicker recipes on busy days, no
repeats where possible, and the `id` links each planned meal to its recipe. If a
day already has a dinner event (dinner out, restaurant, party, BBQ…), that day is
**skipped** (empty name + a short note) rather than assigned a recipe. Empty
library → falls back to fresh ideas.

**Step 2 — `POST /meals/plan/refine`.** Takes the current 7-day plan plus a
free-text change request ("lighter Tuesday", "more veg this week") and returns
the full updated plan — changing only what the request targets. This is what the
mobile Meals tab drives: generate a draft, then iterate by day or whole week.

`GET /meals/prompts` returns the effective prompts for the Settings UI. A shared
`_meal_llm()` helper makes the call, strips markdown fences, and parses; both
routes go through `ai.complete()`, so they require whichever key the selected
model's provider needs (`ai.py`) and return 500 on unset/unparseable output.
`POST /meals/suggest` remains as a backward-compatible alias for step 1.

### Shopping list & AI aisle tagging
`GET /shopping/{week}` derives the week's ingredients from the planned recipes
(skipping leftover/skipped days) and returns them per day, each with a store
**`category`** and a normalized **`qty`** (`{lo, hi, family, unit}` from
`parse_qty`, for summing amounts across days). The clients (mobile, admin,
relay) render one list grouped by category in store-walk order, with day-filter
chips and cumulative per-item amounts; a single tick crosses an item off —
covering both "bought" and "already at home". State is the name-keyed `bought`
set (the legacy `have` field is still accepted by `POST /shopping/{week}` but no
longer used by any client).

Categories come from a single canonical aisle taxonomy (`SHOP_CATEGORIES`,
mirrored by the frontend `SHOP_CATS`). Each ingredient's category is assigned by
Claude and **persisted on the recipe ingredient**, so it's computed once and
reused every week:

- When a plan is saved (`PATCH /meals/plan`), `_ensure_recipe_categories()` tags
  the chosen recipes' ingredients — but only those not already tagged, so
  re-saves cost no AI call. `_ai_categorize_items()` batches the names into one
  Claude call and validates each answer against the taxonomy.
- New recipes arrive pre-tagged: the AI-generation and photo-extraction prompts
  emit a `category` per ingredient.
- `_ingredient_category()`, the original keyword matcher, remains the **instant
  fallback** — used when the API key is unset, a call fails, or a recipe hasn't
  been tagged yet — so the shopping list is never blocked on AI and nothing
  shows uncategorized.

### Cloud shopping relay (use the list at the store)
The shopping list is meant to be used **at the grocery store**, but the NAS is
LAN-only and intentionally not exposed to the internet. So an optional tiny
**relay service** (`shopping-relay/`, its own FastAPI + flat-JSON app, deployed
to a PaaS like Fly.io) acts as the one internet-facing piece. The dataflow is
**outbound-only** from the NAS — exactly like its Anthropic calls:

- `publish_shopping()` on the NAS POSTs `{week, start, days, have, extras}` to the
  relay's `POST /publish`, authenticated with `SHOP_RELAY_PUBLISH_TOKEN`. The
  pushed list is a **rolling 6-day window** from today (`_rolling_shopping_payload()`),
  not a fixed ISO week — so the store view is always the days ahead. It crosses
  ISO-week boundaries (each day's meal is read from whichever week it falls in) and
  merges `have`/`extras` across the weeks the window touches; every day carries its
  ISO `date`, and each ingredient its AI aisle category. The home `GET /shopping/{wk}`
  view (mobile.html) stays on **fixed Mon–Sun weeks** (`_shopping_payload()`) — the
  two share the ingredient/extras helpers. Publishing fires automatically when a plan
  is saved (`PATCH /meals/plan`) or the check-off state changes (`POST /shopping/{week}`),
  and on demand via `POST /shopping/{week}/publish` (the **Publish to phone** button —
  now just a manual refresh, since the window always anchors on today). It's a no-op
  when `SHOP_RELAY_URL` is unset and never fatal on failure. Because each push is
  fire-and-forget, the background sync loop **also re-publishes every cycle**
  (`republish_shopping()`) so a missed/failed push self-heals and the window rolls
  forward each day on its own.
- The relay stores one JSON file — the **current rolling window** (snapshot +
  `bought` state; ticks are carried forward as the window slides and only pruned
  when their item rolls off, never reset by the calendar week turning over), the
  calendar mirror, and the mirrored recipe library — and
  serves `shop.html`: the phone app, **aligned tab-for-tab with mobile.html**
  (Events, Meals, Recipes, Browse, Shopping) over the relay transport. Events =
  add/delete one-off events (queued writes); Meals = read-only plan +
  today/tomorrow recipe; Recipes = import by URL/text (queued); Browse =
  search/filter the mirrored library; Shopping = one merged list with
  day-filter chips, cumulative per-item amounts, and a single tick to cross
  items off. A sixth **AI content** tab covers the live-only features
  (interactive AI planning, reviewed photo scanning): it probes the home
  backend's HTTPS address (`HOME_APP_URL`, no-cors `/healthz` fetch) and, on
  the home network, embeds the real `mobile.html` in an iframe; away from home
  it shows a graceful "needs the home network" panel. The NAS allows the embed
  via `EMBED_ORIGIN` (frame-ancestors CSP), the home hostname must be in
  `ALLOWED_HOSTS`, and the HTTPS-on-LAN setup (DNS name → private IP +
  Let's Encrypt DNS-01, no inbound ports) is documented in
  `HOME_HTTPS_SETUP.md`. **Deliberately not mirrored** to the internet-facing
  box: birthdays and recurring items (managed in the home admin; birth dates
  are the most sensitive data in the stores), recipe photos, and recipe
  provenance (`source`/`log`). The recipe mirror is pushed only when the
  library changes: the NAS sends a `recipesSig` file-mtime signature each
  cycle and re-sends the full blob only when the relay's stored signature
  differs.
  At the store the phone loads it over cellular (online to the *relay*, not the
  NAS), reads `GET /list`, and writes ticks to `POST /state`, caching in
  `localStorage` for spotty signal.
- The phone page is an **installable PWA**: a web manifest + icons give it a
  home-screen icon and full-screen launch, and a service worker (`sw.js`) caches
  the app shell for instant/offline loads (the relay is HTTPS, so service workers
  work — unlike the LAN app). List data is never SW-cached; the page's
  `localStorage` copy handles offline. This is why no native iOS app is needed.
- **Auth** is two long random bearer tokens: `PUBLISH_TOKEN` (the NAS) and
  `DEVICE_TOKEN` (phones), compared in constant time. A phone is provisioned once
  via `https://<relay>/#t=<DEVICE_TOKEN>` (the code is saved locally and stripped
  from the URL). See `shopping-relay/README.md` for deployment.

The NAS remains the home source of truth; bringing store `bought` state back home
is a deliberate non-goal (the relay owns trip state). The NAS opens **no inbound
port** for any of this.

#### Reverse channel — add events / import recipes from anywhere
The same relay carries a **write path** back to the NAS, without ever opening it
up. Since the NAS can't be reached inbound, remote writes go through an **inbox
queue** the NAS drains on an outbound poll:

- The phone queues a command — `POST /inbox {type, payload}` (device **write**
  token) — into the relay's capped `inbox`. Types: `event`, `event_delete`,
  `recipe_import` (`{url|text}`), `recipe_photo` (`{image, media}` — a
  client-side-downscaled ~1280px JPEG as base64), `shopping_extra`
  (`{week, item, action?}` — a manual store-trip item, F9) — aligned to what
  mobile.html can do.
- A background loop on the NAS (`_relay_sync_loop`, started in `startup()`, every
  `SHOP_RELAY_POLL_SECONDS`, default 90) does three things each cycle when the relay
  is configured: `publish_calendar()` mirrors `settings.members` + upcoming events
  (with stable ids, for phone-side deletes) **+ the week's meals** (read straight
  from the display cache — plan, next week, today/tomorrow recipe detail) **+ the
  recipe library** (signature-gated, see above) to `POST /calendar/publish`;
  `republish_shopping()` re-pushes the
  current shopping list so a failed event-driven push self-heals (see above); and
  `drain_relay_inbox()` pulls
  `GET /inbox`, **validates + sanitizes** each command, applies it, then
  `POST /inbox/ack`. Handlers: `_add_event`
  (`_sanitize_event`: ISO date, known member id else `family`, allowed icon, caps);
  `_import_recipe` (SSRF-guarded fetch + `_ai_extract_recipe_text` +
  `_recipe_from_draft`, saved like `POST /recipes`); `_import_recipe_photo`
  (AI vision on the queued photo, original language kept, photo attached to the
  recipe; a likely duplicate is queued under Pending imports (F8c), otherwise
  saved flagged **`needs_review`** — the admin Recipe editor shows a
  "Review needed" banner + amber card highlight until a manual save clears the
  flag; bad image data is dropped, not retried); `_delete_event` (stable id,
  else first exact content match); `_apply_extra` (F9: adds/removes a manual
  shopping item in `shopping.json`, then republishes the week to the phone).
  Retired command types from older phone builds
  (recurring/birthday) are acked and dropped.
- Delivery is **at-least-once + idempotent**: applied command ids are persisted in
  `data/relay_applied.json`, so an apply-succeeded-but-ack-failed item isn't
  re-applied. Malformed commands are acked and dropped (no poison-pill loops).
  A **cost fuse** (mailsync's `EMAIL_FUSE` analogue) executes at most
  `AI_CMD_FUSE` (default 5, env `RELAY_AI_CMD_FUSE`) paid-AI commands
  (`recipe_import`/`recipe_photo`) per cycle — the overflow stays queued for
  later cycles, so a leaked write token can't burn API credit faster than the
  fuse rate.
- Because it's a write path, adding is gated by a **separate `DEVICE_WRITE_TOKEN`**
  scope (the read/shop `DEVICE_TOKEN` can't queue), so a leaked shopping code can't
  inject events. Combined with strict server-side validation and the capped inbox,
  that bounds the blast radius. Edits stay in the home UI.

#### Relay hardening
The relay is the only internet-facing box, so it carries defence-in-depth beyond
the token scopes: a single middleware adds **security headers** and enforces a
**per-IP rate limit** (`RATE_LIMIT`/`RATE_WINDOW`, `/healthz` exempt) and a
**body-size cap** (`MAX_BODY`). The phone page keeps **no inline script** (logic
lives in `/app.js`), so the **CSP is strict** — `script-src 'self'` (no
`unsafe-inline`), plus `connect-src 'self'`, `img-src 'self' data:`,
`frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, HSTS. The
container **runs as a non-root user** and only writes the data volume
(read-only-rootfs compatible). The NAS stays outbound-only, and every home UI
(`admin`/`mobile`/`display`) HTML-escapes event/meal text as a second layer so a
queued entry can't XSS the wall dashboard.

### Mail/calendar sync (`backend/mailsync.py`)
Optional bi-directional sync with a **mailbox.org alias**. Inbound: a background loop polls IMAP, picks out
messages addressed to the alias that carry an iCalendar part, and applies iMIP
`REQUEST`/`CANCEL` (updates via SEQUENCE): daily/weekly rules map natively
onto recurrence v2, anything else expands into 180 days of one-off events.
Outbound: local events/recurring items are diffed by stable id against
`data/mailsync_state.json` (content hash detects edits — `icon` is included
since its emoji rides in the SUMMARY; `iconOnly` excluded)
— new items get a CalDAV `PUT` into a dedicated mailbox.org calendar
(**attendee-free**, so the Open-Xchange server can't send its own duplicate
invitations) plus an iMIP invitation emailed **from the alias** via SMTP;
edits send SEQUENCE-bumped updates on the same UID; deletions send CANCELs.
The owner's member name is appended to outbound copies at build time — the
CalDAV SUMMARY, the email subject, and the body all read e.g. `🦷 Dentist
(Deniz)` (skipped for `who` = family/unknown); it's never stored nor part of
`_content_hash`, so a member rename won't retro-send updates.
Loop prevention: inbound-created ids never sync back out (the outbound diff
subtracts every `st["inbound"][*].local_ids` before comparing), and messages
whose organizer is the alias are ignored; a cycle runs `poll_inbox()` fully —
persisting the inbound mapping per message, and again inside `_apply_request`
right after the events are written so a mid-cycle crash can't leave inbound
events unrecorded — before `sync_outbound()`, both under `_run_lock`. Guard
rails: no-backlog bootstrap, a 20-email-per-cycle fuse, ≤50 inbound messages
per cycle, per-item atomic state writes, everything under the global write
lock. Credentials live only in env (`MAILSYNC_*`); the runtime toggle +
invitees are in `settings.mailSync` (admin → Mail sync, with status + "Sync
now"). Routes: `GET /mailsync/status`, `POST /mailsync/run`. Log category:
`mailsync`.

**Echo-safety runbook** (verify a family member's invite is not bounced back):
send a real invitation from an invitee's own account to the alias, then
`POST /mailsync/run`. Confirm `GET /logs?category=mailsync` shows an
`inbound.request` for that UID and **no** `outbound.invite`; confirm
`data/mailsync_state.json` lists the event under `inbound` (not `outbound`);
run `POST /mailsync/run` a second time and confirm still zero outbound emails
for that UID. Regression coverage: the `test_echo_*` / loop-guard tests in
`backend/tests/test_mailsync.py`.

### Obsidian vault sync (`backend/vault_writer.py`, `backend/vault.py`)
Optional background loop (`vault_sync_loop`, started from `main.py`'s
`startup()` alongside `relay_client`/`mailsync`, gated on `VAULT_COUCHDB_URL`
being set) that projects recipes and calendar data into a CouchDB database
via the Self-hosted LiveSync document format — chunk docs (content-addressed,
`h:t…`) plus entry docs keyed by lowercased vault path, the same reverse-
engineered format `taster/backend/app/couchdb_client.py` established and
every vault-writing project in this ecosystem carries a vendored copy of
(`vault.py` here is one; see `~/.claude/skills/obsidian-vault-writer`).

This is a **different** database from any "security"-side vault a project
might also read/write — see `~/projects/homelab/README.md`'s "The vault
split". `taster` is the only other writer here, and the two don't share any
folder, so there's no owner-tag/frontmatter-prefix contract to follow, just
whole-file ownership of `Recipes/` and `Calendar/`.

Every cycle (`sync_all`, default every 120s via `VAULT_SYNC_POLL_SECONDS`)
rebuilds each note whole from the current JSON on disk — `format_recipe` for
every file in `RECIPES_DIR`, `format_events`/`format_birthdays` for
`events.json` — and lets `LiveSyncVault.project()` decide whether anything
actually changed (a GET + compare before any PUT), so an unchanged recipe or
event costs a read, not a write. No storage-layer hook into `recipes.py` /
`calendar_store.py` was needed: a full rebuild on a timer is simpler and
harder to get subtly wrong than hooking every mutation call site, and at
~70 recipes the whole-corpus reread is cheap.

### Static serving
The `frontend/` directory is mounted at `/` with `html=True`, so the same
server that hosts the API also serves `admin.html`, `mobile.html`, and
`display.html`. Clients derive their API base from `window.location.origin`.

## Frontend (`frontend/`)

Three standalone HTML files — no framework, no bundler. Each is self-contained
(inline CSS + vanilla JS) and talks to the backend over `fetch`.

- **`admin.html`** — the full control panel (largest file). Manage every data
  section: members and their colors, one-off events, birthdays, recurring
  items, the weekly meal plan, and recipes — plus a Settings page (family name,
  colors, meal-planner prompts) and a **Logs** tab (filterable activity trace,
  backed by `/logs`). Recipes are split into two surfaces: **Recipe Library**
  (under Meals) is the browse/search view (the same filter/group UI as
  `recipes.html`, opening the read-only recipe sheet), and **Recipe editor**
  (under Settings) is where recipes are added, edited, imported (Notion .zip,
  link/text, or **📷 photo scan** via `POST /recipes/extract`), and where
  **Pending imports** are compared & merged (F8).
- **`mobile.html`** — a tab-bar phone UI for quick edits on the go (Events,
  Meals, Recipes, **Browse**, Shopping); the Browse tab is a filterable view of
  the recipe library that opens the read-only recipe sheet.
- **`recipes.html`** — a standalone recipe viewer (served statically): a sticky
  filter bar (diacritic-insensitive `fold()` search over name + ingredient
  names, course/cuisine/source/entered-by/max-time/min-rating), results grouped
  by course, and a full detail pane. A pure client of `GET /recipes` +
  `GET /recipes/{id}`; linked from the admin recipe library.
- **`display.html`** — a read-only calendar view for a browser or wall tablet.
  It opens an `EventSource` on `/stream` and live-refreshes when data changes
  (shown by a blinking "live" dot).

## Inky Frame firmware (`_inkyframe/`)

MicroPython for a Raspberry Pi Pico 2 W driving a Pimoroni Inky Frame 7.3"
e-ink panel via `picographics` (`DISPLAY_INKY_FRAME_7`).

| File | Role |
|------|------|
| `main.py` | Entry point. Wake (button/RTC alarm) → fetch → draw → deep sleep; USB fallback polling loop; state persistence, screen dispatch. |
| `network_fetch.py` | WiFi connect + NTP sync, fetch `/display-data` with retries, local cache fallback. |
| `calendar_draw.py` | Renders the rolling four-week grid + legend from the payload. |
| `meals_draw.py` | Renders the meals screen: left = week list (target day boxed), right = that day's recipe (today or tomorrow). |
| `localtime_helper.py` | Stockholm local time with automatic CET/CEST (EU DST). |
| `inky_helper.py` | Low-level board helpers (RTC via PCF85063A, VSYS hold pin, deep sleep). |
| `secrets.py` | WiFi credentials (**not** git-ignored — see security note in README). |
| `word_clock.py`, `news_headlines.py`, `nasa_apod.py`, `daily_xkcd.py`, `carbon_intensity.py` | Additional/standalone draw modules (Pimoroni examples / extras). |
| `main_old.py` | Previous version, kept for reference. |

### Device runtime model
`main.py` is **battery-first**: the board is fully powered off between
refreshes (~µA — the VSYS hold latch is released) instead of running an
always-on loop. An earlier always-on polling build drained 3 AA cells in two
days (~50 mA continuous: MCU awake + WiFi never disconnected); this model
takes the same batteries to months.

1. **Wake** — either a front button (hardware powers the board on and latches
   which button into the shift register, read at boot via `button_x.read()`)
   or the PCF85063A RTC alarm (armed for **00:01 local** → daily refresh).
   Cold boot / no latch = redraw the state persisted in `state.txt`.
2. **Act** — buttons map to screens: **A** = previous four weeks, **B** = home
   (current week on top), **C** = next four weeks, **D** = today's recipe,
   **E** = tomorrow's recipe (the calendar buttons are absolute ±4-week offsets
   from the real current week, not relative to what's displayed). `fetch_data()` retries the server 3× (5s
   apart); on total failure it returns the last `display_cache.json` and the
   payload is flagged `_offline`, which draws a red **OFFLINE** badge.
3. **Sleep** — WiFi is disconnected and deactivated, the PCF85063A is synced
   from NTP (it keeps time across power-off and is the wake source; UTC on the
   chip, converted DST-aware by `localtime_helper`), a daily alarm is armed at
   `(24 − tz_offset):01` UTC ≡ 00:01 local (or a 60-min retry timer if the
   clock was never synced), and `inky_frame.turn_off()` releases the power
   latch. On battery, execution ends there.
4. **USB fallback** — USB power holds VSYS, so `turn_off()` returns and the
   firmware drops into the old button/midnight polling loop; desk and dev
   workflows are unchanged.

Palette pen indices in `make_pens()` are hardware-specific constants confirmed
empirically for this panel. Local time is Stockholm with automatic CET/CEST
switching (`localtime_helper.py`, EU DST rules).

## Data flow: adding an event end to end

1. User adds an event in `admin.html` → `POST /events/add`.
2. Backend appends to `events.json`, re-sorts by date, writes the file.
3. Backend calls `build_display_cache()` → rewrites `cache/display.json`.
4. Backend `broadcast("update", {"section": "events"})`.
5. Any open `display.html` receives the SSE event and re-fetches → repaints.
6. The Inky Frame picks up the change on its next poll (button press or the
   00:01 daily refresh).

## Deployment

A single Python 3.11-slim container installs the pinned dependencies from
`backend/requirements.txt` at start (no Dockerfile/image build yet) and runs
uvicorn on port 8000. `backend/`, `frontend/`,
and `data/` are bind-mounted so edits are live and data persists on the host.
The NAS variant additionally pins a static LAN IP so the microcontroller has a
stable target. Timezone is set to `Europe/Stockholm` in the container.

## Notable constraints & assumptions

- **No auth, open CORS** — intended for a trusted home LAN only.
- **No concurrency control** on JSON writes — fine for single-family use.
- **Week keys are ISO week** (`YYYY-WW`); meal plans are stored per ISO week.
- **Recurring items** are expanded by `_recurring_occurrences()` across the
  rendered four-week window only (jump-ahead keeps it O(grid) for old start dates).
  Multi-day events expand onto each day of their range, capped at 60 days.
  Event times are baked into the payload labels ("14:30 Dentist") so clients
  need no changes; each day lists all-day items first, then timed by time.
- **Recipes are the single source; meals is the planner**: `recipes/` holds the
  full recipe records, and the meal-planning assistant assembles the weekly plan
  by choosing from them. `meals.json` stores only the resulting weekly `plan`.
