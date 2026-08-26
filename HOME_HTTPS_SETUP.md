# Home HTTPS setup — the relay app's "AI content" tab

The relay phone app has an **AI content** tab that embeds the full home app
(`mobile.html`) in an iframe whenever the phone is on the home network — that's
where AI meal planning and the reviewed photo-scan flow live. For the embed to
work, the browser imposes two hard requirements:

1. The relay PWA is served over **HTTPS** (fly.dev), so the iframe target must
   also be HTTPS — `http://10.0.0.2:8000` would be blocked as mixed content.
2. The certificate must be one the phone **already trusts** — no self-signed
   certs (iOS makes those miserable). That means a real Let's Encrypt
   certificate, which needs a real (public) DNS name.

The trick that keeps the NAS un-exposed: use a **public DNS name that resolves
to a private LAN IP**, and get the certificate via a **DNS-01 challenge**
(which proves domain ownership through DNS, never through an inbound
connection). Result: valid HTTPS on the LAN, zero ports opened to the
internet, and the hostname is useless to anyone outside your network.

```
 Phone (home WiFi)                       QNAP NAS
 https://your-name.duckdns.org ──DNS──► 10.0.0.3 (Caddy, qnet)
                                        │ TLS terminated here
                                        ▼
                                     10.0.0.2:8000 (backend container)
```

---

## Step 1 — Create the hostname (DuckDNS, free)

1. Go to https://www.duckdns.org and sign in (GitHub/Google).
2. Create a subdomain, e.g. `your-name` → `your-name.duckdns.org`.
3. Set its IP to the **LAN IP the proxy will use** — `10.0.0.3` in this
   guide (see step 2). Yes, a private IP in public DNS is fine; DuckDNS allows
   it, and it means the name simply doesn't work outside your LAN.
4. Note your DuckDNS **token** (shown at the top of the dashboard) — the
   certificate renewal uses it.

> Own a domain already? Any DNS provider with an API (Cloudflare etc.) works
> the same way — swap the `duckdns` DNS module below for your provider's.

## Step 2 — Run Caddy on the NAS (TLS termination + reverse proxy)

Caddy fetches and **auto-renews** the Let's Encrypt certificate via DNS-01 and
proxies to the backend container. It runs as the **`family-cal-proxy` service
inside the single `family-calendar` compose project** (see
`docker-compose.nas.yml`), on its own static IP on the same `qnet` bridge — so
`:443` never collides with the QTS web UI, and one `./deploy` covers both services.

Its build context (`proxy/` in this repo → `/share/Container/family-calendar-proxy`
on the NAS) holds a `Dockerfile` (stock Caddy has no DuckDNS DNS module, so it's
built once with `xcaddy`) and a `Caddyfile` (fully parameterised — no secrets, no
hostnames). Both, plus a `.env` holding the DuckDNS token, are shipped by:

```sh
./deploy proxy              # production CA
./deploy proxy --staging    # Let's Encrypt staging: untrusted cert, but no rate limit
```

It refuses to run unless `DUCKDNS_DOMAIN` and `DUCKDNS_TOKEN` are in the repo's
`.env`, and it warns if the name doesn't already resolve to the proxy's IP. Then
render and deploy the application YAML as usual:

```sh
./deploy apply                # ships the compose file and applies it over ssh
```

No router port-forwarding. Nothing to renew by hand — Caddy re-issues the
certificate automatically before expiry (DNS-01 again, still no inbound).

> **Rate limits.** Let's Encrypt allows only **5 duplicate certificates per
> hostname per week**. The cert and ACME account key live in the `caddy_data`
> volume; a **Restart** provably reuses them (the expiry doesn't move). If a
> deploy ever wipes that volume, Caddy re-issues and you can burn the
> allowance. The volume name comes from the compose project name, so never rename
> the project. Check with `./deploy check` after any deploy — it prints the issuer
> and expiry, and an unchanged expiry means nothing was re-issued. Use
> `--staging` while debugging DNS or router problems.

> **Alternative** (no extra container): QTS's built-in reverse proxy + the
> myQNAPcloud Let's Encrypt certificate can serve the same purpose, but the
> myQNAPcloud DDNS name points at your **public** IP, so LAN access relies on
> your router supporting NAT hairpinning, and the cert is tied to the
> myqnapcloud.com device name. The Caddy route above keeps all traffic on the
> LAN and is what the rest of this guide assumes.

## Step 3 — Tell the backend to accept the hostname + allow the embed

In the calendar's `.env` on the NAS (`/share/Container/family-calendar/.env`):

```
ALLOWED_HOSTS=localhost,your-name.duckdns.org
EMBED_ORIGIN=https://family-shopping-relay.fly.dev
```

- `ALLOWED_HOSTS`: the backend's DNS-rebinding guard rejects unknown Host
  headers — without this line the proxy gets `421 Misdirected request`.
- `EMBED_ORIGIN`: makes every backend response carry
  `Content-Security-Policy: frame-ancestors 'self' https://family-shopping-relay.fly.dev`,
  which is what permits the relay app (and only it) to iframe the backend.

Push it to the NAS and restart the backend:

```sh
./deploy apply        # pushes .env over ssh (file sync skips dotfiles)
```
`./deploy proxy` rebuilds and restarts it for you.

## Step 4 — Tell the relay where home is

```sh
cd shopping-relay
fly secrets set HOME_APP_URL=https://your-name.duckdns.org
```

(Setting a secret redeploys the machine automatically.) This does two things:
`GET /app-config` hands the URL to the phone app, and the relay's CSP opens
`frame-src`/`connect-src` to exactly that origin.

## Step 5 — Verify

```sh
./deploy check      # probes both apps + the cert (issuer, expiry, trusted?)
```

On a phone/laptop **on home WiFi**:

1. `nslookup your-name.duckdns.org` → must return `10.0.0.3`.
   ⚠️ If it returns nothing/`NXDOMAIN`, your router's **DNS rebind protection**
   is filtering public names that resolve to private IPs (common on Fritz!Box,
   OpenWrt, some ISP boxes). Fix: add `your-name.duckdns.org` to the router's
   rebind-protection exception list, or serve the name from your local DNS
   (Pi-hole/router static entry).
2. `https://your-name.duckdns.org/healthz` in a browser → `{"ok":true}` with a
   valid padlock.
3. Open the relay app → **AI** tab → the full home app should appear embedded.
4. Turn WiFi off (cellular) → AI tab should show the friendly
   "needs the home network" panel; every other tab keeps working.

## Security posture (what this does and doesn't change)

- **No inbound port** is opened; the NAS remains outbound-only to the internet.
- The hostname resolves to a private IP — off-LAN it connects nowhere.
- The certificate is public knowledge (CT logs will show `your-name.duckdns.org`
  exists); that leaks the name only, not reachability.
- The backend still has **no authentication** — HTTPS here is for the browser's
  benefit (mixed-content/iframe rules), not an auth layer. Anyone on your LAN
  could already reach it; that's unchanged.
- The embed is restricted by CSP on both sides: the relay only frames
  `HOME_APP_URL`, the backend only accepts framing from `EMBED_ORIGIN`.
