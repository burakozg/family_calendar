# Runbook — deploying and operating

The day-to-day reference. Every `./deploy` command is documented in
**[DEPLOY.md](DEPLOY.md)**; architecture in [ARCHITECTURE.md](ARCHITECTURE.md); HTTPS
setup in [HOME_HTTPS_SETUP.md](HOME_HTTPS_SETUP.md).

## Everyday commands

| You changed | Do this |
|---|---|
| Python / HTML | `./deploy` |
| `backend/requirements.txt` | `./deploy` |
| a secret in `.env` | `./deploy` |
| `docker-compose.nas.yml` | `./deploy` |
| the `Caddyfile` | `./deploy proxy` |
| the relay (`shopping-relay/`) | bump `sw.js` cache → `fly deploy` (see below) |
| the Mac instance, anything | `./deploy mac` |

That column used to hold a different Container Station click per row — Restart for
some changes, Recreate for others, and picking wrong failed silently. The NAS stack
is a plain compose project deployed over ssh now, and `./deploy` covers every row.

The two rules that made the old table necessary are still true; `./deploy` just
obeys them for you:

- **`env_file` is read when a container is CREATED**, never when it is restarted.
  Change a secret and merely restart, and the container keeps the old value,
  silently. This cost us an outage.
- **Code is bind-mounted, not baked into the image.** So a `.py` edit changes
  neither the image nor the compose config, and a plain `compose up -d` finds
  nothing to do and leaves the old code running — equally silent.

`apply` therefore force-recreates the backend every time, which satisfies both. It
recreates *only* the backend, so HTTPS stays up, and that is only routinely safe
because the MACs are pinned (see `docker-compose.nas.yml`).

## `./deploy`

Run it on the **Mac**. It drives both machines end to end — the NAS half no longer
stops at a UI step.

```sh
./deploy                    # ship the source, then build + apply on the NAS
./deploy mac                # build → stop → remove → recreate → wait for /healthz
./deploy apply              # push .env, ship the compose file, build + up on the NAS
./deploy proxy [--staging]  # ship the Caddy build context + its .env
./deploy data-pull          # NAS → Mac  (one-way by design; backs up first)
./deploy check              # health-probe both + verify the TLS cert
```

`./deploy apply` renders `docker-compose.nas.yml` into
`deploy-out/docker-compose.nas.yml` (git-ignored, regenerated on demand), ships it
to the NAS as its `docker-compose.yml`, and brings the stack up over ssh. It reads
the real owner of the NAS `data/` and rewrites `user:` to match — the container
runs non-root with a read-only rootfs, so a uid mismatch is an instant crash loop.
It also fills in the qnet addresses and their pinned MACs, so no real address is
ever in a tracked file.

Everything needing ssh only works on the **home LAN or the VPN**. Set up a key once,
or you'll retype the password on every step:

```sh
ssh-copy-id -p 44 admin@nas.local
```

## Where things live

| | |
|---|---|
| NAS host (ssh) | `nas.local`, port **44** |
| Backend container | `10.0.0.2:8000` (qnet bridge) |
| Caddy / HTTPS | `10.0.0.3` → `https://your-name.duckdns.org` |
| NAS app root | `/share/Container/family-calendar` |
| NAS proxy root | `/share/Container/family-calendar-proxy` |
| Relay (cloud) | `https://family-shopping-relay.fly.dev` |
| Mac instance | `http://localhost:8000` |

The NAS runs **one compose project with two services** (`family-calendar` +
`family-cal-proxy`), so one `./deploy` covers both. The project is named
`family-calendar` and that name is load-bearing: compose derives
`family-calendar_caddy_data` from it, and that volume holds the Let's Encrypt
certificate.

## How the NAS gets everything (QSync is OFF)

The QSync pair for this folder is **disabled**. Everything the NAS needs is shipped
explicitly over ssh — nothing arrives by magic, and nothing arrives half-written:

| What | How |
|---|---|
| `backend/`, `frontend/` | `./deploy ship` |
| `.env` | `./deploy apply` |
| `docker-compose.nas.yml` | `./deploy apply` (rendered, then shipped) |
| Caddy `Dockerfile` / `Caddyfile` / its `.env` | `./deploy proxy` |
| `data/` | never copied to the NAS — **the NAS owns it.** `./deploy data-pull` brings it *here* |

`./deploy sync-check` answers "is the NAS running the same source as this Mac?" —
cheap, and worth running before any Restart.

`ship` is a true **mirror**: it tars the source over, then deletes any `.py`/`.html`
on the NAS that no longer exists here (`tar -x` only overlays — and a stale Python
file is worse than a missing one, because it still imports). It also purges the
`__pycache__` trees QSync left behind.

### Why QSync was abandoned

It failed five distinct ways here, and each one cost real debugging time:

1. `.env` left **two months stale** — it skips dotfiles.
2. `data/recipes/` **never delivered at all** — *Access denied* on the dirs the
   container owns. The NAS ran for months with no recipe library.
3. `backend/tests/` the same, and never retried even after the ownership was fixed.
4. A `main.py` **frozen between two edits**: the route called a function whose import
   line hadn't arrived. The app booted cleanly and raised `NameError` on one code path
   only — the worst kind of failure, because everything looked healthy.
5. It then **ignored a `touch`** and still didn't re-deliver the file.

A two-way syncer that silently half-delivers source is a liability, not a
convenience. Don't turn it back on.

## Which instance owns the data

**The NAS is the single source of truth.** It is the only instance wired to the
cloud relay and to mailsync, so it is the only one that can accept a write from the
phone or from email.

**The Mac is a test box** — and, while travelling, a temporary server for the Inky
Frame. It has **no** `SHOP_RELAY_*` and **no** `MAILSYNC_*` in `docker-compose.yml`,
deliberately: those aren't inert settings, each starts a background loop that mutates
shared state. Two relay-connected instances would each drain the phone's queued
writes, ack them, and never see the other's — the datasets diverge silently. Don't
add them back "for parity".

So the data flows one way, on demand:

```sh
./deploy data-pull         # NAS → Mac
```

That's the whole thing — it stops the Mac container, backs up the local `data/`,
pulls, and restarts the container if it had been running. It's an **exact mirror**:
the new tree is extracted and swapped in, so a recipe deleted on the NAS doesn't
linger here (`tar -x` only overlays, and `rebuild_recipe_index()` globs every
`*.json`, so a leftover would come back as a phantom recipe).

No restart is needed for the data itself, incidentally — the backend reads each JSON
store from disk per request and holds nothing in memory. The stop/start is to swap
the directory safely under the bind mount.

There is **no `data-push`**. Anything you change on the Mac is test data and will be
overwritten by the next pull. Real edits belong in the NAS app or the phone.

Because the Mac never touches the relay, **both instances can run at once** — the
Mac's copy simply goes stale until you pull again. This isn't a fast-moving dataset;
pull when you need it.

### Travelling with the Inky Frame

The Inky can be pointed at the Mac while away — the NAS keeps running at home,
unchanged.

1. `./deploy data-pull`, so the Mac has current data.
2. Point the Inky at the Mac: `NAS_URL` in `_inkyframe/network_fetch.py` →
   `http://<mac-lan-ip>:8000/display-data` (`ipconfig getifaddr en0`).
3. Back home: set `NAS_URL` to `http://10.0.0.2:8000/display-data` again.

Two things that will bite:

- **A sleeping Mac serves nothing.** The Inky refreshes at 00:01; if the Mac is
  asleep it shows the last cached payload with an **OFFLINE** badge. `caffeinate -s`
  if you want overnight refreshes.
- **`NAS_URL` is a raw IP, so it changes on every network you join.** Use the IP, not
  a `.local` name — the backend's Host allowlist (`ALLOWED_HOSTS`) returns `421` for
  unknown hostnames, while a direct IP always passes.

## Inky Frame — Turkish/Swedish characters

The device draws with PicoGraphics' `bitmap8`, which has glyphs only for ASCII
32–126. Turkish (`ı ş ğ ç ö ü`) and Swedish (`å ä ö`) render as garbage. Rather than
rebuild the firmware, the backend folds the payload to ASCII **at serve time**, and
only when the device asks: `/display-data?ascii=1` (`ASCII_ONLY` in
`_inkyframe/network_fetch.py`). The stored cache, the web UI and the phone keep
proper Unicode — only the e-ink screen sees `Tavuklu Kisir`.

If you change the fold, note that `ı` and `İ` have **no Unicode decomposition**, so
an NFKD-only fold drops them silently instead of mapping them; they're handled by an
explicit table first.

## The relay (cloud PWA)

```sh
cd shopping-relay && fly deploy
```

⚠️ **Bump `const CACHE = 'shop-shell-vN'` in `sw.js` with every change to
`shop.html` or `app.js`.** The shell is served cache-first and the service worker
only re-fetches it when `sw.js`'s own bytes change. Skip the bump and returning
phones keep the old UI while NAS-side data updates fine — a genuinely confusing
split.

The shell is served `no-cache` and the app reloads once when a new service worker
takes over, so a deploy now lands on the **next launch**. If a phone is still stuck,
fully quit it (swipe away), or re-add it from the setup link:

```
https://family-shopping-relay.fly.dev/#t=<DEVICE_TOKEN>&w=<DEVICE_WRITE_TOKEN>
```

`&w=` is optional — without it the phone gets read/shop scope only. The tokens live
in the URL **fragment**, which never reaches the server or its logs. They are Fly
secrets (write-only — you cannot read them back), so keep a copy somewhere safe.

## HTTPS / the certificate

Real Let's Encrypt cert, issued via a **DNS-01** challenge against DuckDNS —
ownership is proven with a DNS TXT record, never an inbound connection. **No port is
forwarded; the NAS stays closed.** The hostname is public but resolves to a private
LAN IP, so it reaches nothing from outside.

- **A Restart does not re-issue.** Caddy reuses the cert from its volume and renews
  only ~30 days before expiry. Verify with `./deploy check`: an unchanged `notAfter`
  means nothing was re-issued.
- Production allows only **5 duplicate certificates per hostname per week**. Use
  `./deploy proxy --staging` (untrusted cert, no rate limit) while debugging DNS.
- Don't put `ca {$ACME_CA}` in the Caddyfile. An unset variable expands to nothing,
  leaving a bare `ca` that Caddy refuses to parse. `--staging` *adds* the line.
- If the name resolves to nothing on a phone, it's the router's **DNS-rebind
  protection** dropping public names that point at private IPs. Add an exception.

## Gotchas, learned the hard way

- **`env_file` needs a Recreate, not a Restart.** (Worth repeating.) `./deploy`
  force-recreates the backend for exactly this reason.
- **A `.py` edit changes neither the image nor the compose config**, because the
  source is bind-mounted — so `compose up -d` alone finds nothing to do and leaves
  the old code running. Same fix, same reason.
- **Resource limits can live in `docker-compose.nas.yml` now.** Container Station
  rejected them in pasted YAML and wanted them in its Advanced Settings panel;
  that constraint went with the Application wrapper. Nothing is set yet.
- **`docker compose config` expands `env_file` back into `environment:`** — so it
  can't be used to render a secret-free YAML. The NAS compose is secret-free at
  source instead.
- **Pin MACs for anything on the qnet macvlan.** Docker randomises the MAC on every
  create; the router then answers for the old one and the container comes back
  healthy, correctly routed, and unreachable inbound for minutes.
- **QNAP has no working `scp`/sftp.** Files go over ssh with `cat >` / `tar`.
- **Don't re-enable QSync for this folder.** It half-delivered source and cost hours;
  everything now ships explicitly (see above).
- **The Mac is arm64, the QNAP is x86_64.** Each builds its own image from the same
  Dockerfile; don't try to cross-build and ship one.
- **`10.0.0.2` unreachable** almost always means the app is *stopped*, not a
  network problem.
