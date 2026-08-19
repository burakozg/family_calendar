// Service worker for the shopping-list PWA.
// Caches only the static app shell so the page loads instantly and works offline.
// The list data (/list, /state) is never cached here — the page keeps its own
// localStorage copy and handles offline/sync itself.
const CACHE = 'shop-shell-v19';  // bumped: serve app.js network-first (see the fetch handler)
const SHELL = [
  '/',
  '/app.js',
  '/manifest.webmanifest',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // Only handle same-origin GETs; let the API and everything else go to network.
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  // Never cache dynamic/authenticated API endpoints.
  if (['/list', '/state', '/calendar', '/recipes', '/inbox', '/app-config'].includes(url.pathname)) return;

  // Navigations: network-first, fall back to the cached shell when offline.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')));
    return;
  }

  // App code: network-first, like navigations. Cache-first is wrong for the one
  // file that changes every release: a phone kept serving the previous app.js
  // while loading the current markup, so the UI showed a new toggle whose code
  // wasn't running and the flag it sets never reached the payload. The stale
  // worker had no reason to update, and nothing on screen hinted at the split.
  // Costs one request per launch when online; still fully offline-capable.
  if (url.pathname === '/app.js') {
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Everything else in the shell (icons, manifest) is versioned by CACHE and
  // changes rarely: cache-first, then network.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => hit))
  );
});
