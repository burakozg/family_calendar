# `./deploy` — command reference

Run it **on the Mac**. Everything that touches the NAS goes over ssh, so you need the
home LAN or the VPN.

A bare **`./deploy` is `ship` then `apply`** — the everyday NAS deploy. It used to
be refused, on the grounds that shipping `.env` without the source is a trap; it
isn't one now, because bare `./deploy` does both. `mac` still has to be asked for
by name: that track targets a different machine entirely.

These verbs — `ship`, `apply`, `check`, `--no-apply` — are the same in all four
NAS projects; see `homelab/README.md` for the shared contract. `ship` was
`push-src` and `apply` was `nas`.

There is no Restart-vs-Recreate decision to make any more: the NAS stack is a
plain compose project deployed over ssh, and `./deploy` does the whole thing.
Operating context (the certificate, the Inky, which instance owns the data) is in
[RUNBOOK.md](RUNBOOK.md).

---

## The commands

### `./deploy mac`
Build `family-calendar:mac`, **stop, remove and recreate** the local container, wait
for `/healthz`. Fully automated — the Mac is the one machine we own end to end.

Pins the container's uid/gid to the owner of `data/`. It runs non-root with a
read-only rootfs, so `data/` is the only writable path and a uid mismatch would
crash-loop it.

The Mac is a **test box**. It has no `SHOP_RELAY_*` and no `MAILSYNC_*` — see
`docker-compose.yml`, and *Which instance owns the data* in the runbook.

---

### `./deploy ship`
Ship `backend/` + `frontend/` to the NAS over ssh. **This is how code reaches the
NAS** — QSync is not used (see below).

A true **mirror**: it tars the source over, then deletes any `.py`/`.html` on the NAS
that no longer exists here, and purges stale `__pycache__`. (`tar -x` only overlays,
and a stale Python file is worse than a missing one — it still imports.) Self-verifies
with `sync-check` when it's done.

`ship` deliberately does not restart anything — use bare `./deploy` (ship then
apply) to make the change live. Python loads its modules at process start and the
source is bind-mounted, so a shipped file does nothing until the process is
replaced; `apply` handles that (see below).

---

### `./deploy apply`
1. **Pushes `.env`** over ssh (`cat >` + atomic `mv` — QNAP has no working `scp`),
   and checks the byte count landed.
2. **Pins `user:`** to the real owner of the NAS `data/`.
3. **Renders** `deploy-out/docker-compose.nas.yml` — real qnet addresses and MACs
   filled in from `.deploy.env` — and ships it as the NAS's `docker-compose.yml`.
4. **`docker compose up -d --build`** over ssh: builds both images on the NAS
   (this Mac is arm64, the QNAP x86_64) and starts whatever changed.
5. **Force-recreates the backend.** `up -d` will not, and must: `backend/Dockerfile`
   only installs `requirements.txt`, so the source is bind-mounted and never in
   the image. Edit a `.py` file and neither the image nor the compose config
   changes — `up -d` finds nothing to do and Python keeps running the code it
   loaded at start. This is the step the old "now press Restart" instruction was.
   A *recreate* rather than a restart because `env_file` is read when a container
   is **created**: a rotated secret needs a new container, not a new process.
6. **Verifies over the LAN** and checks the certificate. "Up (healthy)" is judged
   from inside the container against localhost and is not the same claim.

Only the backend is force-recreated, so HTTPS stays up. That is only routinely
safe because the MACs are pinned — see `docker-compose.nas.yml`.

The compose file is **secret-free**: the containers read secrets from the NAS-side
`.env` via `env_file`.

---

### `./deploy proxy [--staging]`
Ship the Caddy build context (`proxy/Dockerfile`, `proxy/Caddyfile`) and write its
`.env` (the DuckDNS token, mode 600) on the NAS. Backs up the existing Caddyfile/.env
to `.bak` first — they're all that stands between you and a working certificate.

It then rebuilds and restarts the proxy itself — `up -d --build` for a changed
`Dockerfile`, then an explicit `restart`, because the Caddyfile is a bind-mounted
*file* rather than part of the compose config and `up -d` would see nothing to do.
Finishes by printing the certificate's issuer and expiry.

`--staging` uses Let's Encrypt's **staging** CA: an untrusted cert, but effectively
unlimited retries. Use it while debugging DNS or the router — production allows only
**5 duplicate certificates per hostname per week**.

Checks that the DuckDNS name resolves to the proxy's IP first, and warns about
router DNS-rebind protection, which is the usual failure.

`--staging` works by **inserting a `ca` directive into the Caddyfile** before
shipping it, not by setting an env var — see `proxy/Caddyfile`'s header for why
(`ca {$ACME_CA}` with the variable unset expands to a bare `ca` that Caddy won't
parse, and `env_file` is only read on container *creation* anyway). It's inserted
with `awk`, because BSD `sed` — which is what macOS has — reads neither `\t` in a
pattern nor `\n` in a replacement, and would quietly produce a Caddyfile with no
`ca` line at all: a production certificate issued while you believed you asked
for staging, burning one of the five weekly slots.

`BACKEND_ADDR` in the proxy's `.env` is written as `$APP_LAN_IP:$APP_PORT` — the
app container's own qnet address, since the proxy sits on the same bridge.

---

### `./deploy data-pull`
Refresh this Mac's `data/` **from** the NAS. Stops the Mac container, backs up the
local `data/` to `~/family-calendar-data-backup-<timestamp>`, streams the NAS copy
over ssh, verifies the recipe count, and restarts the container if it had been
running.

**One direction only, by design.** The NAS is the single source of truth: it is the
only instance wired to the cloud relay and to mailsync, so it is the only one that can
accept a write from the phone or from email. **There is no `data-push`** — the Mac
must never overwrite the truth. Anything you change on the Mac is test data and will
be overwritten by the next pull.

An exact **mirror**: the tree is extracted and swapped in, so a recipe deleted on the
NAS doesn't linger here (`rebuild_recipe_index()` globs every `*.json`, so a leftover
would come back as a phantom recipe).

---

### `./deploy sync-check`
md5s every runtime `.py`/`.html` on both sides and names what differs. Answers *"is
the NAS running the same source as this Mac?"*

Cheap. **Worth running before any Restart** — it turns a confusing runtime error into
a one-line answer.

---

### `./deploy check`
Health-probe both instances and verify the TLS certificate — printing its issuer and
expiry. An **unchanged `notAfter`** after a deploy is the proof that
Caddy reused the cert rather than re-issuing it (which would spend one of the five
weekly duplicates).

---

## Common sequences

**Changed Python or HTML**
```sh
./deploy               # ship + apply; the backend is recreated for you
./deploy mac               # if you want it on the Mac too
```

**Changed a secret in `.env`**
```sh
./deploy apply         # .env is re-read because the backend is recreated
```

**Changed `backend/requirements.txt`**
```sh
./deploy               # --build rebuilds the image for a requirements.txt change
./deploy mac               # rebuilds locally
```

**Changed `docker-compose.nas.yml`**
```sh
./deploy apply         # ships the compose file and applies it
```

**Refresh the Mac with real data**
```sh
./deploy data-pull
```

**"Is the NAS actually running my code?"**
```sh
./deploy sync-check
./deploy check
```

---

## Settings

Overridable by environment variable — but don't export these by hand every
session. Copy `deploy.env.example` to `.deploy.env` (git-ignored) and fill in
your real values once; `./deploy` sources it automatically before applying
the defaults below, and `docker-compose.nas.yml`'s two qnet IPs are rendered
from the same file (see its header comment and `./deploy apply`).

| Var | Default | What |
|---|---|---|
| `NAS_SSH` | `admin@nas.local` | NAS host login |
| `NAS_SSH_PORT` | `44` | QNAP's non-standard ssh port |
| `APP_LAN_IP` | `10.0.0.2` | the **container's** IP on the qnet bridge — not the NAS host |
| `APP_PORT` | `8000` | backend port |
| `NAS_APP_DIR` | `/share/Container/family-calendar` | app directory on the NAS |
| `PROXY_ROOT` | `/share/Container/family-calendar-proxy` | proxy's directory on the NAS |
| `PROXY_LAN_IP` | `10.0.0.3` | Caddy's IP on the qnet bridge |
| `MAC_PORT` | `8000` | local backend port |

**Set up an ssh key** or every command will prompt, several times each:

```sh
ssh-copy-id -p 44 admin@nas.local
```

---

## Why nothing uses QSync

The sync pair for this folder is **off**. Everything the NAS needs is shipped
explicitly, because QSync failed five distinct ways here:

1. `.env` left **two months stale** — it skips dotfiles.
2. `data/recipes/` **never delivered at all** — *Access denied* on the dirs the
   container owns. The NAS ran for months with no recipe library.
3. `backend/tests/` the same, and never retried even after the ownership was fixed.
4. A `main.py` **frozen between two edits** — the route called a function whose import
   line hadn't arrived. The app booted cleanly and raised `NameError` on one code path
   only, which is the worst kind of failure: everything looked healthy.
5. It then **ignored a `touch`** and still didn't re-deliver.

A two-way syncer that silently half-delivers source is a liability, not a convenience.
Don't turn it back on.
