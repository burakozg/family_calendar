# Wall tablet setup (e.g. Galaxy Tab S10 Ultra)

`frontend/tablet.html` is a native, large-screen, **read-only** display view —
the tablet's counterpart to the Inky Frame. It shows the same rolling 4-week
calendar and meal plan as the e-ink panel and `display.html`, laid out for a
big landscape touchscreen instead of an 800x480 e-ink panel, with a dark
ambient mode so it can sit on an AMOLED screen 24/7 without burning in.

This doc is the Android-side setup — the parts that live outside the repo and
would otherwise be undiscoverable a year from now.

## URL to use

```
https://your-name.duckdns.org/tablet.html
```

Use the **LAN-HTTPS hostname**, not `http://<nas-ip>:8000/tablet.html`. This
app already runs a Caddy proxy on the NAS (see `HOME_HTTPS_SETUP.md`) that
serves everything except `admin.html` over a real, browser-trusted
certificate — a public DNS name that simply resolves to a private LAN IP, with
no ports forwarded and nothing reachable from outside the house. Chrome only
grants the camera (for motion-wake) and `navigator.wakeLock` (to keep the
panel powered on) to a *secure context*, and only persists that camera grant
per-origin on one — a self-signed cert or the raw IP won't get either.
Fullscreen works either way, but there's no reason to give up the other two.

This hostname only resolves on the home WiFi — make sure the tablet is
connected to it, not to mobile data.

If the Caddy proxy is ever down, `tablet.html` still renders correctly over
plain `http://<nas-ip>:8000/tablet.html` — it just falls back to touch-to-wake
with no camera motion detection, which the page handles silently.

## First-time setup

1. Open the URL above in Chrome.
2. Grant the camera permission when prompted (motion-wake). If you'd rather
   skip it, deny it — the page falls back to touch-to-wake and nothing else
   changes. Frames are processed on-device only: never uploaded, never stored.
3. Menu (⋮) → **Add to Home screen**, so it launches like an app.
4. Tap anywhere on the page once to trigger fullscreen (removes Chrome's URL
   bar) — a hint at the bottom of the screen says so until you do.

## Android settings

- **Settings → Developer options → Stay awake** (on, while charging). Needed
  because `tablet.html` deliberately does *not* try to blank the Android
  screen itself — see "Why the screen stays on" below. Enable Developer
  options first if not already: Settings → About tablet → tap Build number
  7 times.
- **Settings → Battery → Protect battery** (caps charge around 85%). The
  tablet stays on USB‑C power indefinitely; without this a lithium battery
  held at 100% for years degrades and can swell.
- Auto-rotate **off**, locked to landscape.
- **Do Not Disturb** on, so a notification shade never covers the calendar.
- Keep the tablet on the home WiFi network (see URL note above).

## Screen pinning (keeps it locked to this one app)

Settings → Security and privacy → More security settings → **App pinning** →
enable it. Then, with Chrome open on the tablet page: open Recent Apps, tap
the app icon at the top of the Chrome card, choose **Pin**. To unpin (e.g. to
do maintenance), hold Back + Recent Apps together (exact gesture varies by
Android version — check on-screen once pinned).

## Why the screen stays on (motion-wake, the web-page version)

Some kiosk apps (e.g. Fully Kiosk Browser) turn the Android screen itself off
and wake it from a background camera service. A plain web page in Chrome
can't do that — once Android sleeps the display, the page stops running and
can't see anyone approach. So this setup inverts it:

- The **Android screen stays on** permanently (Stay awake + the page's own
  wake lock as a backup).
- The **page** dims itself instead: after 5 minutes of no touch/motion it
  dims, after 12 minutes it drops to a minimal black clock-only view, after
  20 minutes it goes fully black. On AMOLED, black pixels are *off* — that's
  where the burn-in protection and most of the power saving actually come
  from, not from the Android backlight.
- It wakes instantly on **touch**, and — if you granted the camera — on
  **motion** sensed by the front camera (a coarse 64x48 frame-difference, a
  few times a second, entirely on-device).
- The whole layout also drifts a few pixels every so often on a slow cycle,
  so no edge sits on the exact same pixels for hours at a stretch.

If wall placement makes touch-to-wake awkward (e.g. it's mounted high) and you
want a true screen-off with instant camera wake, that's the point to look at
Fully Kiosk Browser (~€12) instead — it can point at the same URL.

## Known limits vs. a dedicated kiosk app

This is plain Chrome + screen pinning, not a kiosk launcher, so:

- **No auto-start after a reboot** — after a power cut, someone has to
  manually reopen and re-pin Chrome on the tablet.
- **No crash recovery** — if Chrome itself crashes, nothing restarts it.
- No remote admin, no scheduled hard screen-off.

All of the above are things Fully Kiosk Browser handles; none of them are
addressed by this setup on purpose, to keep the tablet side to "install Chrome
correctly" rather than a second app to maintain.
