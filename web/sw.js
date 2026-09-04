/* Deckster service worker (P4).
 * Makes the control surface an installable PWA (fullscreen, add-to-home-screen)
 * and available offline. Network-first for every GET so updates always propagate
 * on reload (the agent is local — fresh is effectively free); the cache is the
 * offline fallback. The WebSocket is never cached — it always goes to the agent.
 */
var CACHE = "streamctl-shell-v19";
var SHELL = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/manifest.webmanifest"
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== CACHE) return caches.delete(k);
      }));
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  // Never intercept the WebSocket or health/API calls.
  if (url.pathname === "/ws" || url.pathname === "/health") return;

  // Network-first for everything: the agent is local (USB/localhost), so fresh is
  // effectively free and updates ALWAYS propagate on reload. The cache is only an
  // offline fallback. (Cache-first previously pinned stale app.js/style.css until a
  // second reload — the source of "my change isn't showing on the phone".)
  e.respondWith(
    fetch(req).then(function (res) {
      if (res && res.status === 200 && url.origin === location.origin) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) { return hit || caches.match("/"); });
    })
  );
});
