// Service worker for the installed app (PWA). It exists mainly so the browser will offer "Install" on
// Windows/Android/desktop Chrome, and to keep the install usable for a moment when the connection drops.
//
// This is a business app with live financial and stock data, so pages themselves are deliberately NOT
// cached: an operator must never see yesterday's stock level or an old balance while offline and act on
// it. Only the static app shell (CSS, JS, icons, the offline page) is cached, and every real page request
// goes to the network first; offline.html is shown only when that fails.
//
// The app's own CSS and JS carry the deployment in their address (?v=...), so a copy of one never goes out of date: it is
// served from the cache with no trip to the network at all, and copies of older deployments are dropped as newer ones
// arrive. Anything without a version (icons, the offline page) is served from the cache and refreshed in the background.
//
// Bump CACHE_NAME when the shell's own caching behaviour changes (not for every CSS/JS edit - those are
// already cache-busted by the app's own ?v= on each asset() URL).
// v2: the MS-POS name and tomato seedling icons replaced the old ones under the same addresses
// v3: the app's versioned CSS and JS come from the cache with no trip to the network, one version of each kept
const CACHE_NAME = "ms-pos-shell-v3";
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

// Keeps one version of each file: the one just stored
function keepOnly(cache, url) {
    return cache.keys().then((requests) => Promise.all(requests.map((old) => {
        const oldUrl = new URL(old.url);
        return oldUrl.pathname === url.pathname && oldUrl.search !== url.search ? cache.delete(old) : null;
    })));
}

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
        const versioned = url.searchParams.has("v");
        event.respondWith(
            caches.match(request).then((cached) => {
                if (cached && versioned) return cached;   // this exact version never changes
                const fetched = fetch(request).then((response) => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy).then(() => versioned ? keepOnly(cache, url) : null));
                    }
                    return response;
                }).catch(() => cached);
                // Unversioned files: the cached copy at once, refreshed in the background for next time
                return cached || fetched;
            })
        );
    }
});
