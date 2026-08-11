# Family Calendar

A self-hosted family calendar that syncs to a **Pimoroni Inky Frame 7" e-ink display** and is managed from any browser. It shows a monthly calendar with per-person color-coded events, Swedish red days, and an AI-assisted weekly meal plan.

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
| `backend/` | FastAPI app, split into focused modules (see ARCHITECTURE.md): JSON-file storage, SSE stream, display-cache builder, AI meal planner + recipe extraction/import, shopping-aisle tagging, activity log, cloud-relay sync, mailbox.org mail sync. |
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
what to Restart vs Recreate, the `./deploy` commands, file-sync and certificate
rules, and the gotchas worth not rediscovering.

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
but on **different Docker daemons** (Docker Desktop vs Container Station), so they
never clobber each other; the images are tagged `family-calendar:mac` and
`family-calendar:nas`. One script drives both, from the Mac:

```bash
./deploy                    # both tracks
./deploy mac                # build, stop, remove, recreate the local container; wait for /healthz
./deploy nas                # prepare the NAS deploy (see below)
./deploy proxy [--staging]  # ship the Caddy/HTTPS assets (see RUNBOOK.md)
./deploy data-pull          # refresh the Mac's data/ from the NAS (see File sync, below)
./deploy check              # health-probe both, and verify the TLS cert
```

**Mac** is fully automated. It also pins the container's uid/gid to the owner of
`data/` — the container is non-root with a read-only rootfs, so only `data/` is
writable and a uid mismatch would crash-loop it.

**NAS** runs the app as a Container Station **Application**, so create/recreate/
start/stop happen in the Container Station UI, not over the CLI. `./deploy nas`
therefore does the parts that *can* be automated and hands you the rest:

1. **Pushes `.env` over ssh** — file sync skips dotfiles, so the NAS copy goes
   stale; without this the container starts with no API key and no relay tokens.
   Streamed via `ssh 'cat >'` (QNAP has no working `scp`/sftp), written to a temp
   file and `mv`'d into place (atomic, and works even though the existing `.env`
   is owned by another user).
2. **Pins `user:`** to the real owner of `/share/Container/family-calendar/data`.
3. **Renders the YAML** to paste, and copies it to the clipboard.

Then: *Container Station → Applications → family-calendar → Recreate*, select all,
paste, Recreate. It builds the image on the NAS. Verify with `./deploy check`.

The pasted YAML is **secret-free**: `docker-compose.nas.yml` uses
`env_file: /share/Container/family-calendar/.env`, so nothing sensitive lands in
Container Station's stored config (its Inspect view shows the YAML in plaintext).
Every var the backend reads has a code default, so only `TZ` — which the OS uses,
not the app — stays in the file. Re-run `./deploy nas` whenever a secret changes,
then Recreate.

`docker-compose.nas.yml` is **one application with two services**: the calendar
backend on `10.0.0.2`, and a Caddy reverse proxy (`family-cal-proxy`) on
`10.0.0.3` that gives the home app a real HTTPS certificate — both on the
external `qnet` bridge, so the Inky Frame can reach the backend reliably and
Caddy's `:443` can't collide with the QTS web UI. One Recreate deploys both. The
proxy's own `Dockerfile` / `Caddyfile` / `.env` live outside the synced tree and
are shipped by `./deploy proxy`; see **[HOME_HTTPS_SETUP.md](HOME_HTTPS_SETUP.md)**.

It keeps the obsolete `version: "3"` key — harmless on Compose v2, but Compose v1
needs it to parse the v3 schema at all, and Container Station's vintage is unknown.

### Getting the source onto the NAS

QSync is **not** used — it half-delivered source files (see RUNBOOK.md). Everything is
shipped explicitly over ssh:

```bash
./deploy push-src     # backend/ + frontend/ → NAS (a true mirror; then Restart)
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

- **A** — Previous month
- **B** — Home (calendar, current month)
- **C** — Next month
- **D** — Today's recipe
- **E** — Tomorrow's recipe

Month buttons are absolute offsets from the real current month, not relative
to what's on screen.

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
| GET | `/display-data?month_offset=N` | Pre-rendered payload for the Inky Frame |
| POST | `/display-data/rebuild` | Force-rebuild the display cache |
| GET | `/stream` | SSE stream of `update` events for web clients |
| GET | `/healthz` | Liveness probe (also used by the relay app's AI content tab to detect the home network) |
| GET | `/export` | Download a zip of all JSON stores + recipes (restore: unzip into `data/`) |
| GET | `/export/recipes` | Download a recipes-only zip (records + index + photos) |

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
