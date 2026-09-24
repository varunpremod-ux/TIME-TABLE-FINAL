/* Jaegers Hub - service worker.
   Two rules:
   1. App shell (this page, its icons, the manifest) - cache-first. It barely changes, so serve
      it instantly from cache and only hit the network to keep the cache fresh in the background.
   2. Data (every *.json - timetable, mess bill, attendance, announcements, key dates, and the
      per-roll files) - network-first with a cache fallback. A good connection always gets the
      latest numbers; a bad one falls back to the last successful load instead of a dead
      "couldn't load" screen.
   The page itself (index.html / "./") is network-first too, so an updated index.html shows up
   on the next load instead of being stuck behind an old cached copy; the cache is only the
   offline fallback. Icons and the manifest stay cache-first (they never change).
   Bump CACHE below (v2 -> v3 ...) if a future change needs to force everyone's cache to clear. */
const CACHE = 'jaegers-hub-v2';
const APP_SHELL = ['./', './index.html', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  const isPage = req.mode === 'navigate' || url.pathname.endsWith('/') || url.pathname.endsWith('.html');
  if (isPage || url.pathname.endsWith('.json')) {
    event.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(cache => cache.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then(
      cached =>
        cached ||
        fetch(req).then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(cache => cache.put(req, copy));
          return res;
        })
    )
  );
});
