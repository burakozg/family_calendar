# `./deploy` — command reference

Run it **on the Mac**. Everything that touches the NAS goes over ssh, so you need the
home LAN or the VPN. There is **no default command**: they target different machines,
and a bare `./deploy` that shipped `.env` but not the source would be a trap.

Operating context (what to Restart vs Recreate, the certificate, the Inky) is in
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

### `./deploy push-src`
Ship `backend/` + `frontend/` to the NAS over ssh. **This is how code reaches the
NAS** — QSync is not used (see below).

A true **mirror**: it tars the source over, then deletes any `.py`/`.html` on the NAS
that no longer exists here, and purges stale `__pycache__`. (`tar -x` only overlays,
and a stale Python file is worse than a missing one — it still imports.) Self-verifies
with `sync-check` when it's done.

→ then **Container Station → Restart**. Python loads its modules at process start, so
a bind-mounted file change does nothing until the process restarts.

---

### `./deploy nas`
Three things Container Station can't do for itself:

1. **Pushes `.env`** over ssh (`cat >` + atomic `mv` — QNAP has no working `scp`),
   and checks the byte count landed.
2. **Pins `user:`** to the real owner of the NAS `data/`.
3. **Renders the YAML** to `deploy-out/nas-app.yml` and copies it to the clipboard.

→ then **Container Station → Recreate**, paste, Recreate.

The YAML is **secret-free** — the container reads secrets from the NAS-side `.env` via
`env_file`, so nothing sensitive lands in Container Station's stored config, which its
Inspect view shows in plaintext.

⚠️ A changed secret needs a **Recreate, not a Restart**: `env_file` is read when a
container is *created*. Restart and it silently keeps the old value.

---

### `./deploy proxy [--staging]`
Ship the Caddy build context (`proxy/Dockerfile`, `proxy/Caddyfile`) and write its
`.env` (the DuckDNS token, mode 600) on the NAS. Backs up the existing Caddyfile/.env
to `.bak` first — they're all that stands between you and a working certificate.

→ then **Container Station → Restart** (the Caddyfile is bind-mounted).

`--staging` uses Let's Encrypt's **staging** CA: an untrusted cert, but effectively
unlimited retries. Use it while debugging DNS or the router — production allows only
**5 duplicate certificates per hostname per week**.

Checks that the DuckDNS name resolves to the proxy's IP first, and warns about
router DNS-rebind protection, which is the usual failure.

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
expiry. An **unchanged `notAfter`** after a Restart or Recreate is the proof that
Caddy reused the cert rather than re-issuing it (which would spend one of the five
weekly duplicates).

---

## Common sequences

**Changed Python or HTML**
```sh
./deploy push-src          # → Container Station → Restart
./deploy mac               # if you want it on the Mac too
```

**Changed a secret in `.env`**
```sh
./deploy nas               # → Container Station → RECREATE (not Restart)
```

**Changed `backend/requirements.txt`**
```sh
./deploy push-src          # → Container Station → RECREATE (the image must rebuild)
./deploy mac               # rebuilds locally
```

**Changed `docker-compose.nas.yml`**
```sh
./deploy nas               # → paste → Recreate
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
from the same file (see its header comment and `./deploy nas`).

| Var | Default | What |
|---|---|---|
| `NAS_SSH` | `admin@nas.local` | NAS host login |
| `NAS_SSH_PORT` | `44` | QNAP's non-standard ssh port |
| `NAS_HOST` | `10.0.0.2` | the **container's** IP on the qnet bridge — not the NAS host |
| `NAS_PORT` | `8000` | backend port |
| `NAS_ROOT` | `/share/Container/family-calendar` | app directory on the NAS |
| `PROXY_ROOT` | `/share/Container/family-calendar-proxy` | proxy's directory on the NAS |
| `PROXY_IP` | `10.0.0.3` | Caddy's IP on the qnet bridge |
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
