// Service worker for the installed app (PWA): what lets the browser offer "Install", and what keeps the app usable on a bad network day.
//
// Pages: every page is fetched from the server first, so what is shown is live. A copy of each page opened while signed in is kept on
// the device; when a page cannot be fetched (offline), or takes more than SLOW_MS on a very slow connection, the copy is shown instead,
// with a banner saying it is a copy and from when, and nothing on it can be saved until the connection is back. A page never opened on
// this device shows the offline screens (offline.html: the device's snapshot, and a till that works offline). The copies belong to the
// person signed in: another person signing in, or signing out, clears them (offline-store.js tells this worker who is signed in).
//
// The app's own CSS and JS carry the deployment in their address (?v=...), so a copy of one never goes out of date: it is served from
// the cache with no trip to the network at all, and copies of older deployments are dropped as newer ones arrive. Anything without a
// version (icons, the offline screens) is served from the cache and refreshed in the background.
//
// Bump CACHE_NAME when the shell's own caching behaviour changes (not for every CSS/JS edit - those are already cache-busted by ?v=).
// v2: the MS-POS name and tomato seedling icons replaced the old ones under the same addresses
// v3: the app's versioned CSS and JS come from the cache with no trip to the network, one version of each kept
// v4: pages kept for offline viewing, and the offline screens
// v5: the offline screens' files kept under their bare path, and refreshed while the app is online
// v6: the icons redrawn with more room around them, under the same addresses; the offline screens can log plantings
// v7: the offline screens print receipts on a regular or a receipt printer (print.js)
// (Push notifications, below, change no caching.)
const CACHE_NAME = "ms-pos-shell-v7";
const PAGES = "ms-pos-pages-v1";
const OFFLINE_URL = "/static/offline.html";
const PRECACHE = [OFFLINE_URL, "/static/offline-app.js", "/static/offline-store.js", "/static/print.js", "/static/styles.css", "/static/mobile.css",
                  "/static/icons/icon.svg", "/static/icons/icon-192.png", "/static/icons/icon-512.png"];
const SLOW_MS = 6000;      // a page taking longer than this shows its saved copy, if there is one
const KEEP_PAGES = 150;    // pages kept; the oldest go first
// Pages never kept: signing in and out, the assistant, alerts, downloads, backups and other things that are not a page to look at
const NOT_KEPT = /^\/(login|logout|setup|signup|demo|assistant|notifications\/(poll|\d+\/open)|cron|offline\/|backup|manifest|sw\.js)|\.(csv|pdf|xlsx|zip)$/;

self.addEventListener("install", (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME && key !== PAGES).map((key) => caches.delete(key))))
            .then(() => self.clients.claim())
    );
});

// Who is signed in (offline-store.js says, on every page): another person means the kept pages were not theirs. It also means the app
// is open with a connection, so the offline screens' own files are brought up to date (at most every 10 minutes): after a deployment
// they match the app.
let shellAt = 0;
function refreshShell() {
    if (Date.now() - shellAt < 10 * 60 * 1000) return Promise.resolve();
    shellAt = Date.now();
    return caches.open(CACHE_NAME).then((cache) => Promise.all(PRECACHE.map((path) =>
        fetch(path, { cache: "no-store" }).then((response) => (response.ok ? cache.put(path, response) : null)).catch(() => null))));
}

self.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type !== "user") return;
    event.waitUntil(Promise.all([
        caches.open(CACHE_NAME).then((cache) => cache.match("/__user").then((was) => (was ? was.text() : null)).then((id) => {
            if (id === String(data.id)) return null;
            return caches.delete(PAGES).then(() => cache.put("/__user", new Response(String(data.id))));
        })),
        refreshShell(),
    ]));
});

// Keeps one version of each file: the one just stored (the unversioned copy the offline screens use is left alone)
function keepOnly(cache, url) {
    return cache.keys().then((requests) => Promise.all(requests.map((old) => {
        const oldUrl = new URL(old.url);
        return oldUrl.pathname === url.pathname && oldUrl.search && oldUrl.search !== url.search ? cache.delete(old) : null;
    })));
}

function isPage(request, url) {
    if (url.pathname.startsWith("/static/") || NOT_KEPT.test(url.pathname)) return false;
    return request.mode === "navigate" || request.headers.get("X-Nav-Page") === "1";
}

// A page worth keeping: one of the app's own, signed in (it has the app's header), as HTML
function keep(request, response) {
    if (!response.ok || response.redirected || !(response.headers.get("content-type") || "").includes("text/html")) return;
    const copy = response.clone();
    copy.text().then((html) => {
        if (!html.includes('id="app-header"') || !html.includes('id="page-main"')) return;
        return caches.open(PAGES).then((cache) => cache.put(request.url, new Response(html, {
            headers: { "Content-Type": "text/html; charset=utf-8", "X-Saved-At": String(Date.now()) },
        })).then(() => cache.keys()).then((keys) => Promise.all(keys.slice(0, Math.max(0, keys.length - KEEP_PAGES)).map((k) => cache.delete(k)))));
    }).catch(() => { /* not kept this time */ });
}

// The kept copy, with a banner saying what it is. why: "offline" or "slow"
function savedCopy(url, why) {
    return caches.open(PAGES).then((cache) => cache.match(url)).then((saved) => {
        if (!saved) return null;
        const at = new Date(Number(saved.headers.get("X-Saved-At")) || Date.now());
        const stamp = at.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + ", " + at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        return saved.text().then((html) => {
            const note = why === "slow"
                ? "The connection is very slow, so this is the copy of this page from " + stamp + ". Open it again in a moment for the latest."
                : "You are offline. This is the copy of this page from " + stamp + ": nothing can be saved on it until the connection is back.";
            const banner = '<div class="flash notice no-print" id="offline-copy">' + note +
                ' <a href="/static/offline.html" data-full-reload>Work offline</a> (sell, and see stock, customers and invoices)</div>';
            return new Response(html.replace('<main id="page-main">', '<main id="page-main">' + banner), {
                headers: { "Content-Type": "text/html; charset=utf-8", "X-Offline-Copy": why },
            });
        });
    });
}

function offlineScreens() {
    return caches.match(OFFLINE_URL);
}

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;   // forms (sales, purchases...) always go straight to the network

    const url = new URL(request.url);
    if (url.origin !== location.origin) return;

    if (isPage(request, url)) {
        // Live from the server; the kept copy only when the server cannot be reached, or is far too slow
        event.respondWith(new Promise((resolve) => {
            let answered = false;
            const answer = (response) => { if (!answered && response) { answered = true; resolve(response); } };
            const fallback = (why) => savedCopy(request.url, why).then((copy) => copy || (why === "offline" ? offlineScreens() : null));
            const slow = setTimeout(() => { fallback("slow").then(answer); }, SLOW_MS);
            fetch(request).then((response) => {
                clearTimeout(slow);
                keep(request, response);
                answer(response);
            }).catch(() => {
                clearTimeout(slow);
                fallback("offline").then((copy) => answer(copy || Response.error()));
            });
        }));
        return;
    }

    if (url.pathname.startsWith("/static/")) {
        const versioned = url.searchParams.has("v");
        // A versioned file is that exact version; an unversioned one (the offline screens') is kept under its bare path, never mistaken
        // for some versioned copy of the same file
        event.respondWith(
            caches.match(versioned ? request : url.pathname).then((cached) => {
                if (cached && versioned) return cached;   // this exact version never changes
                const fetched = fetch(request).then((response) => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(versioned ? request : url.pathname, copy).then(() => versioned ? keepOnly(cache, url) : null));
                    }
                    return response;
                }).catch(() => cached);
                // Unversioned files: the cached copy at once, refreshed in the background for next time
                return cached || fetched;
            })
        );
    }
});

// ---------- Push notifications ----------
// An alert sent to this device (Settings > Alerts > Turn on notifications) arrives here even with the app closed: it is shown, and the
// app's icon gets the unread count. Tapping it opens what the alert is about (and marks it read), in an open window of the app if there is one.
self.addEventListener("push", (event) => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "MS-POS", body: event.data ? event.data.text() : "" }; }
    const badge = self.navigator && self.navigator.setAppBadge
        ? (data.unread ? self.navigator.setAppBadge(data.unread) : self.navigator.clearAppBadge()).catch(() => null)
        : Promise.resolve();
    event.waitUntil(Promise.all([
        badge,
        self.registration.showNotification(data.title || "MS-POS", {
            body: data.body || "", tag: data.tag || undefined, data: { url: data.url || "/notifications" },
            icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png",
        }),
    ]));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const url = new URL((event.notification.data && event.notification.data.url) || "/notifications", self.location.origin).href;
    event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
        const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
        if (open) return open.navigate(url).then((w) => (w || open).focus()).catch(() => self.clients.openWindow(url));
        return self.clients.openWindow(url);
    }));
});
