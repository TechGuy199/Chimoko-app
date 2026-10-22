// Service worker for the installed app (PWA). It exists mainly so the browser will offer "Install" on
// Windows/Android/desktop Chrome, and to keep the install usable for a moment when the connection drops.
//
// This is a business app with live financial and stock data, so pages themselves are deliberately NOT
// cached: an operator must never see yesterday's stock level or an old balance while offline and act on
// it. Only the static app shell (CSS, JS, icons, the offline page) is cached, and every real page request
// goes to the network first; offline.html is shown only when that fails.
//
// Bump CACHE_NAME when the shell's own caching behaviour changes (not for every CSS/JS edit - those are
// already cache-busted by the app's own ?v= on each asset() URL).
// v2: the MS-POS name and tomato seedling icons replaced the old ones under the same addresses
const CACHE_NAME = "ms-pos-shell-v2";
const OFFLINE_URL = "/static/offline.html";
const PRECACHE = [OFFLINE_URL, "/static/icons/icon-192.png", "/static/icons/icon-512.png"];

self.addEventListener("install", (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;   // forms (sales, purchases...) always go straight to the network

    const url = new URL(request.url);
    if (url.origin !== location.origin) return;

    if (request.mode === "navigate") {
        // A real page: always try the network first, so data is never stale. Only on failure (no connection)
        // does the cached offline page appear.
        event.respondWith(
            fetch(request).catch(() => caches.match(OFFLINE_URL))
        );
        return;
    }

    if (url.pathname.startsWith("/static/")) {
        // The app shell: serve from cache instantly, refresh the cache in the background from the network
        event.respondWith(
            caches.match(request).then((cached) => {
                const fetched = fetch(request).then((response) => {
                    if (response.ok) caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
                    return response;
                }).catch(() => cached);
                return cached || fetched;
            })
        );
    }
});
