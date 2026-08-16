// Service worker for the shopping-list PWA.
// Caches only the static app shell so the page loads instantly and works offline.
// The list data (/list, /state) is never cached here — the page keeps its own
// localStorage copy and handles offline/sync itself.
const CACHE = 'shop-shell-v18';  // bumped: 3-month event split + years on dates; sturdier photo decode
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

  // Static assets: cache-first, then network (and cache the result).
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => hit))
  );
});
