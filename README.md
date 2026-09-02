# Family Calendar

A self-hosted family calendar that syncs to a **Pimoroni Inky Frame 7" e-ink display** and is managed from any browser. It shows a rolling four-week calendar — the current week always on top — with per-person color-coded events, Swedish red days, and an AI-assisted weekly meal plan.

Everything runs on a home server (a QNAP NAS in this deployment) inside a single Docker container. A battery-powered Pico 2 W pulls a pre-rendered payload from the server and draws it on the e-ink screen; web clients get live updates over Server-Sent Events.

> **Security note:** the backend has no login and no CORS grant — it relies entirely on staying unreachable from the internet (LAN-only, Host-header allowlisted; see ARCHITECTURE.md/RUNBOOK.md). **Never port-forward it or otherwise expose it directly to the internet.** Remote/mobile access is meant to go through `shopping-relay/` instead, which is the one piece designed to face the internet.

```
┌──────────────┐   HTTP    ┌─────────────────┐   HTTP    ┌────────────────┐
│  Web clients │◄────────► │  FastAPI backend│◄────────► │ Inky Frame      │
│ admin/mobile │   +SSE    │  (Docker/NAS)   │  (poll)   │ Pico 2 W e-ink │
│  /display    │           │  JSON storage   │           │ (MicroPython)  │
└──────────────┘           └─────────────────┘           └────────────────┘
```

## Components

| Path | What it is |
|------|-----------|
| `backend/` | FastAPI app, split into focused modules (see ARCHITECTURE.md): JSON-file storage, SSE stream, display-cache builder, AI meal planner + recipe extraction/import, shopping-aisle tagging, activity log, cloud-relay sync, mailbox.org mail sync, hobby-vault sync. |
| `frontend/admin.html` | Full admin UI — manage members, events, birthdays, recurring items, meals, recipes. |
| `frontend/mobile.html` | Phone-friendly UI for quick edits (incl. a Browse tab for the recipe library). |
| `frontend/recipes.html` | Standalone recipe viewer — live filters (search, course, cuisine, time, rating) + detail. |
| `frontend/display.html` | Browser version of the calendar view (live via SSE). |
| `_inkyframe/` | MicroPython firmware for the Pico 2 W + Inky Frame 7" hardware. |
| `shopping-relay/` | Optional tiny cloud service hosting the phone app (aligned tab-for-tab with mobile.html): shopping list at the store, events, recipe import/scan + browse from anywhere, plus an **AI content** tab that embeds the full home app on the home network (see its README + `HOME_HTTPS_SETUP.md`) — all without exposing the NAS. |
| `data/` | Runtime JSON data (settings, events, meals, recipes, shopping, display cache). Git-ignored. |
| `docker-compose.yml` | Local run config. |
| `docker-compose.nas.yml` | QNAP NAS run config (static IP on `qnet` bridge). |

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together and the data
model, **[DEPLOY.md](DEPLOY.md)** for the `./deploy` command reference, and
**[RUNBOOK.md](RUNBOOK.md)** for the day-to-day operating reference —
the `./deploy` commands, file-sync and certificate
rules, and the gotchas worth not rediscovering.

## What's new

Highlights only — `git log` is the full history.

<!-- Add an entry only when user-visible functionality changes (not fixes or
     refactors). Newest first, keep it to ~8; drop the oldest but keep the
     initial release. If this ever falls badly out of date, delete it rather
     than half-fix it — a wrong shop window is worse than none. -->

- Recipe editor flags near-duplicates however a recipe was entered, and a duplicate
  name can no longer overwrite an existing recipe ([`532596d`](../../commit/532596d))
- Meal-kit sheets that arrive as one page are split into a main and a side, attributed
  to "Meal kit" rather than to a person
  ([`938c194`](../../commit/938c194), [`f2b5ff3`](../../commit/f2b5ff3))
- Inky calendar rolls as a four-week window — the current week stays on top instead of
  today walking down the screen ([`4ce67b6`](../../commit/4ce67b6))
- Separate AI models for reading photos and for text/planning, so each job can use a
  model that suits it ([`9c0a696`](../../commit/9c0a696))
- Phone app collapses far-future events, shows years, and can translate scanned
  recipes ([`1ff3e93`](../../commit/1ff3e93))
- Meal planner skips dinner only for events that actually cover it, and spreads
  chicken / fish / red meat / vegetable across the weekdays ([`3dfefe8`](../../commit/3dfefe8))
- Initial release: self-hosted calendar, e-ink display, AI meal planning
  ([`99b47e5`](../../commit/99b47e5))

## Requirements

- Docker + Docker Compose (server side)
- An Anthropic API key (used by the AI features: meal planning, recipe
  extraction/import, unit normalization, and shopping-aisle tagging — the
  calendar itself works without it)
- A Pimoroni Inky Frame 7.3" with a Raspberry Pi Pico 2 W (optional — the web UI works without hardware)

## Running the server

1. Create a `.env` file in the project root (template: `.env.example`):

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   # Optional — cloud relay: shopping list at the store + add events from anywhere (see shopping-relay/):
   # SHOP_RELAY_URL=https://your-relay.fly.dev
   # SHOP_RELAY_PUBLISH_TOKEN=...
   # SHOP_RELAY_POLL_SECONDS=90
   # EMBED_ORIGIN=https://your-relay.fly.dev   # allow the relay app's AI tab to embed this app (see `HOME_HTTPS_SETUP.md`)
   ```

2. Start it:

   ```bash
   docker compose up -d
   ```

   This builds a small image (`backend/Dockerfile`: Python + the pinned
   dependencies from `backend/requirements.txt`) and runs it hardened:
   non-root, read-only root filesystem (only `data/` is writable), no
   privilege escalation, bounded memory. Code and data stay bind-mounted,
   so edits remain live.

   The container runs as UID:GID `1000:1000` by default — make sure `data/`
   is writable by that user (`chown -R 1000:1000 data/`), or set `APP_UID` /
   `APP_GID` in `.env` to the owner of `data/`. After changing
   `backend/requirements.txt`, rebuild with `docker compose up -d --build`.

3. Open the UIs:

   - Admin:   `http://<host>:8000/admin.html`
   - Mobile:  `http://<host>:8000/mobile.html`
   - Display: `http://<host>:8000/display.html`

   The frontends detect their API base from `window.location.origin`, so no
   configuration is needed as long as they're served by the backend.

### Deploying — `./deploy`

Mac and NAS are two independent tracks. They build from the same `backend/Dockerfile`
but on **different Docker daemons** (Docker Desktop vs the NAS's), so they
never clobber each other; the images are tagged `family-calendar:mac` and
`family-calendar:nas`. One script drives both, from the Mac:

```bash
./deploy                    # ship the source, then build + apply on the NAS
./deploy mac                # build, stop, remove, recreate the local container; wait for /healthz
./deploy apply              # push .env, ship the compose file, build + up on the NAS
./deploy proxy [--staging]  # ship the Caddy/HTTPS assets (see RUNBOOK.md)
./deploy data-pull          # refresh the Mac's data/ from the NAS (see File sync, below)
./deploy check              # health-probe both, and verify the TLS cert
```

**Mac** is fully automated. It also pins the container's uid/gid to the owner of
`data/` — the container is non-root with a read-only rootfs, so only `data/` is
writable and a uid mismatch would crash-loop it.

**NAS** is a plain compose project deployed over ssh, so `./deploy` drives it end
to end — no UI step. It used to be a Container Station **Application**, which meant
the deploy stopped at "render the YAML, paste it, press Recreate"; see
`docker-compose.nas.yml`'s header for why that changed. `./deploy apply`:

1. **Pushes `.env` over ssh** — file sync skips dotfiles, so the NAS copy goes
   stale; without this the container starts with no API key and no relay tokens.
   Streamed via `ssh 'cat >'` (QNAP has no working `scp`/sftp), written to a temp
   file and `mv`'d into place (atomic, and works even though the existing `.env`
   is owned by another user).
2. **Pins `user:`** to the real owner of `/share/Container/family-calendar/data`.
3. **Renders** the compose file — real qnet addresses and pinned MACs filled in
   from `.deploy.env` — and ships it to the NAS.
4. **Builds and starts** it there with `docker compose up -d --build`, then
   force-recreates the backend so it re-reads the bind-mounted source and `.env`.
5. **Verifies over the LAN** and checks the certificate.

The compose file is **secret-free**: it uses
`env_file: /share/Container/family-calendar/.env`, so nothing sensitive is in the
YAML. Every var the backend reads has a code default, so only `TZ` — which the OS
uses, not the app — stays in the file. Re-run `./deploy` whenever a secret changes;
the backend is recreated, which is what makes `env_file` take effect.

`docker-compose.nas.yml` is **one compose project with two services**: the calendar
backend on `10.0.0.2`, and a Caddy reverse proxy (`family-cal-proxy`) on
`10.0.0.3` that gives the home app a real HTTPS certificate — both on the
external `qnet` bridge, so the Inky Frame can reach the backend reliably and
Caddy's `:443` can't collide with the QTS web UI. One `./deploy` covers both. The
proxy's own `Dockerfile` / `Caddyfile` / `.env` live outside the synced tree and
are shipped by `./deploy proxy`; see **[HOME_HTTPS_SETUP.md](HOME_HTTPS_SETUP.md)**.

Its `name: family-calendar` pins the compose project, and that name is
load-bearing: compose derives `family-calendar_caddy_data` from it, and that volume
holds the Let's Encrypt certificate and ACME account key.

### Getting the source onto the NAS

QSync is **not** used — it half-delivered source files (see RUNBOOK.md). Everything is
shipped explicitly over ssh:

```bash
./deploy ship     # backend/ + frontend/ → NAS (a true mirror; then Restart)
./deploy sync-check   # is the NAS running the same source as this Mac?
./deploy data-pull    # NAS data/ → this Mac, on demand (one direction only)
```

The NAS owns `data/` and is the single source of truth: it is the only instance wired
to the cloud relay and to mailsync. The Mac is a test box — see the comment in
`docker-compose.yml` for why `SHOP_RELAY_*` and `MAILSYNC_*` are deliberately absent
from it.

## Setting up the Inky Frame

The `_inkyframe/` directory contains MicroPython code that runs on the Pico 2 W.

1. Flash the Pimoroni Inky Frame MicroPython build to the Pico.
2. Copy the contents of `_inkyframe/` to the device (e.g. with Thonny).
3. Edit `_inkyframe/secrets.py` with your WiFi credentials.
4. Set `NAS_URL` in `_inkyframe/network_fetch.py` to your server's
   `/display-data` endpoint.
5. Reset the device; `main.py` runs on boot.

**Buttons on the frame:**

- **A** — `< 4 Weeks` (the previous four-week window)
- **B** — `Home` (calendar, current week on top)
- **C** — `4 Weeks >` (the next four-week window)
- **D** — Today's recipe
- **E** — Tomorrow's recipe

The calendar is a rolling four-week window: row one is always the current
Monday-start week, so today's marker crosses the top row and never walks down
the screen. A and C slide that window a whole screenful (±4 weeks) and are
absolute offsets from the real current week, not relative to what's on screen.

The display also auto-refreshes at 00:01 local time (RTC-alarm wake). If the
server is unreachable it falls back to the last cached payload and shows an
**OFFLINE** badge.

On battery the board is fully powered off between refreshes and woken by the
buttons or the RTC alarm, so a set of 3 AA cells lasts months; on USB it runs
an always-on polling loop instead (see ARCHITECTURE.md → Device runtime
model).

## HTTP API (summary)

| Method | Path | Purpose |
|--------|------|---------|
| GET/POST | `/settings` | Family members, display, meal prompt |
| GET | `/ai/models` | Selectable AI models + configured providers (drives the admin picker) |
| GET | `/ai/usage` | Token consumption per day and per model (`?days=14`), from the append-only ledger |
| GET/POST | `/events` | Full events document |
| POST/DELETE | `/events/add`, `/events/by-id/{id}` | One-off events (`/events/{idx}` = legacy index delete) |
| POST/DELETE | `/birthdays/add`, `/birthdays/by-id/{id}` | Birthdays (`/birthdays/{idx}` = legacy) |
| POST/DELETE | `/recurring/add`, `/recurring/by-id/{id}` | Recurring items, e.g. garbage day (`/recurring/{idx}` = legacy) |
| GET/PATCH | `/meals`, `/meals/plan` | Weekly meal plan (keyed by ISO week) |
| GET | `/meals/prompts` | Effective meal-planner prompts (settings values or defaults) |
| POST | `/meals/plan/generate` | Step 1 — AI drafts the week from the standing prompt + recipe library |
| POST | `/meals/plan/refine` | Step 2 — AI updates the plan from a change request (one day or the whole week) |
| GET/POST/PUT/DELETE | `/recipes`, `/recipes/{id}` | Recipe library (the source of meal options) |
| POST | `/recipes/extract` | Photograph a recipe → AI extracts structured fields |
| POST | `/recipes/extract-text` | Paste a recipe URL or text → AI extracts structured fields (SSRF-guarded fetch) |
| POST | `/recipes/import-link` | One-shot import: URL (site/Instagram) or pasted text → AI-extract and save (original language kept; translation opt-in) |
| POST | `/recipes/import-notion` | Bulk import from a Notion "Markdown & CSV" export .zip (base64) |
| POST | `/recipes/ai-generate` | Generate and save AI recipes (marked `source: ai`) |
| POST | `/recipes/normalize-units` | Standardize units (g/kg, °C, cups/tbsp/tsp) across recipes |
| POST | `/recipes/classify-courses` | Assign a `course` (main/side/soup/…) to recipes missing one — batched AI call with keyword fallback |
| POST | `/recipes/reindex` | Rebuild `index.json` from the recipe files (picks up new index fields) |
| GET | `/recipes/similar` | Offline duplicate check (`?name=&source_type=&source_value=`) for the editor |
| GET/POST | `/recipes/pending`, `/recipes/pending/{pid}` | Queued import drafts awaiting a compare & merge |
| POST | `/recipes/pending/{pid}/resolve` | Resolve a pending draft: `merge` (chosen fields), `create`, or `discard` |
| POST | `/meals/suggest` | Backward-compatible alias of `/meals/plan/generate` |
| GET/DELETE | `/logs` | Activity log for the admin trace UI (filter via query params) |
| GET | `/logs/meta` | Log filter options (categories, levels, whos seen) |
| GET/POST | `/shopping/{week}` | Week's ingredients grouped by AI-tagged store aisle + `have`/`bought` state |
| POST | `/shopping/{week}/publish` | Push the rolling 6-day list to the cloud relay ("Publish to phone" — the phone view always shows today + next 5 days); no-op unless `SHOP_RELAY_URL` is set |
| GET/POST | `/mailsync/status`, `/mailsync/run` | mailbox.org mail/calendar sync status + manual cycle (see ARCHITECTURE.md's "Mail/calendar sync" section) |
| GET | `/display-data?week_offset=N` | Pre-rendered payload for the Inky Frame (`N` in weeks; the older `month_offset` is still accepted, 4 weeks per month, so a device on un-reflashed firmware keeps working) |
| POST | `/display-data/rebuild` | Force-rebuild the display cache |
| GET | `/stream` | SSE stream of `update` events for web clients |
| GET | `/healthz` | Liveness probe (also used by the relay app's AI content tab to detect the home network) |
| GET | `/export` | Download a zip of all JSON stores + recipes (restore: unzip into `data/`) |
| GET | `/export/recipes` | Download a recipes-only zip (records + index + photos) |

## Obsidian vault sync

Optional (`VAULT_COUCHDB_URL` unset = the loop never starts, see
`.env.example`). Projects recipes and calendar events into the **hobby**
Obsidian vault — a different vault from any "security" one; see
`~/projects/homelab/README.md`'s "Writing into the vault" and
`~/.claude/skills/obsidian-vault-writer` for the shared contract this
follows and `taster`, its only other writer.

| | |
|---|---|
| Reads | `data/recipes/*.json`, `data/events.json` — never modified |
| Writes | `Recipes/<id>.md` (one per recipe), `Calendar/Events.md`, `Calendar/Birthdays.md` |
| Model | Whole-file ownership — nothing else writes these folders, so no owner tag or frontmatter prefix is needed |

Rebuilt whole on every cycle (`backend/vault_writer.py::sync_all`, every
`VAULT_SYNC_POLL_SECONDS`, default 120s), not appended to — a deleted or
edited recipe has to be able to disappear or change in the vault, and only a
full rebuild does that for free. Cheap when nothing changed: each note is a
content-hash compare before any write, so a quiet cycle costs reads, not
writes (`backend/vault.py`, vendored from the same LiveSync client every
other vault-writing project in this ecosystem carries a copy of).

## Tests

`./run-tests.sh` runs the backend suite (pytest; fast, no network). Install
dev deps first: `pip install -r backend/requirements.txt -r backend/requirements-dev.txt`.

## ⚠️ Security note

Secrets live only in git-ignored files: `.env` (API key — start from
`.env.example`) and `_inkyframe/secrets.py` (WiFi credentials — start from
`_inkyframe/secrets.py.example`). Never commit real values; if either file
has ever been shared or synced elsewhere, rotate the key/password.

The backend has no authentication — only run it on a trusted LAN. It is same-origin only (no CORS), and a
Host-header allowlist blocks DNS-rebinding: direct IP access always works,
while hostnames must be listed in `ALLOWED_HOSTS` (comma-separated; default
`localhost`) — add your hostname there if you put a reverse proxy in front
(the recommended HTTPS-on-LAN proxy setup for the relay app's AI content tab
is documented in `HOME_HTTPS_SETUP.md`, together with `EMBED_ORIGIN`, which
allows that one origin to iframe this app).

The **shopping relay** (`shopping-relay/`) is the one component that is meant to
face the internet, and it is token-gated. The NAS stays inbound-closed: it only
makes **outbound** calls to the relay. Keep `PUBLISH_TOKEN`/`DEVICE_TOKEN` long
and random, and rely on the PaaS-provided HTTPS.
