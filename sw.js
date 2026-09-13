// Only caches the static shell (the page itself, manifest, icons) so the
// app can install and open instantly. It deliberately does NOT cache
// anything from the backend API — attendance and schedule data must
// always come from the network, live, never a stale cached copy.
const CACHE_NAME = 'ug-lms-shell-v1';
const SHELL_FILES = ['./index.html', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Only ever intervene for same-origin GETs of the shell files above —
  // everything else (in particular, every call to the backend API on a
  // different origin) goes straight to the network, untouched.
  if (url.origin !== self.location.origin || event.request.method !== 'GET') {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
