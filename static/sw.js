/*
 * AttendQR scanner service worker.
 *
 * The scanner is installed as a PWA (manifest start_url = /scan) and is
 * expected to keep working when the venue Wi-Fi drops. The previous version
 * only ever tried `caches.match()` without putting anything IN the cache, so
 * nothing was ever cached and a reload with no network killed the scanner.
 *
 * Strategy:
 *   - Precache the scanner shell + the QR library at install time.
 *   - Static assets: cache-first (fast, and works offline).
 *   - Navigations: network-first, falling back to the cached /scan shell.
 *   - /api/ requests: NEVER cached. A stale roster or a replayed scan result
 *     would be worse than an honest network error — the page already queues
 *     failed scans in localStorage and syncs them later.
 */
const CACHE_NAME = 'attendqr-v2';

const PRECACHE_URLS = [
  '/scan',
  '/static/style.css',
  '/static/audio.js',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      // addAll rejects the whole batch if any single request fails (e.g. the
      // CDN is unreachable), so add individually and tolerate misses.
      .then((cache) => Promise.all(
        PRECACHE_URLS.map((url) => cache.add(url).catch(() => null))
      ))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Attendance data is never served from cache.
  if (url.pathname.startsWith('/api/')) return;

  // Page loads: try the network, fall back to the cached scanner shell.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match(request).then((hit) => hit || caches.match('/scan')))
    );
    return;
  }

  // Assets: serve from cache, refresh in the background.
  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response && response.status === 200 && response.type !== 'opaque') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      });
    })
  );
});
