// Offline: what this device keeps so the app can still be used on a bad network day, and the sales made meanwhile.
// Loaded once on every signed-in page (base.html), and by the offline screens (offline.html). In this browser's own database (IndexedDB):
// - a snapshot of the business: POS products and prices, stock, customers, the last 30 days of invoices, plantings and pre-orders,
//   refreshed from the server every few minutes while there is a connection;
// - the sales made offline, each with a key made here, uploaded one by one when the connection is back (the server records a key once,
//   however often it is sent). One the server turns down waits under "Needs attention" instead of being lost.
// A small pill shows what is waiting. Signing out wipes the snapshot and the saved pages; sales not yet uploaded stay, to upload when
// the person who made them signs in again.
(function () {
    "use strict";
    const me = document.currentScript ? document.currentScript.dataset : {};
    const DB = "ms-offline";
    const PAGES = "ms-pos-pages-v1";   // the pages the service worker keeps (sw.js)
    const FRESH_MS = 10 * 60 * 1000;   // a snapshot older than this is refreshed when the app is open with a connection
    const listeners = [];
    let opening = null, uploading = null;
    let unreachable = null;    // the server could not be reached the last time it was asked (Wi-Fi on but no internet, say); null: not asked yet

    function open() {
        if (opening) return opening;
        opening = new Promise(function (resolve, reject) {
            const req = indexedDB.open(DB, 1);
            req.onupgradeneeded = function () {
                req.result.createObjectStore("meta", { keyPath: "k" });
                req.result.createObjectStore("outbox", { keyPath: "key" });
            };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
        return opening;
    }

    function store(name, mode, work) {
        return open().then(function (db) {
            return new Promise(function (resolve, reject) {
                const tx = db.transaction(name, mode);
                const result = work(tx.objectStore(name));
                tx.oncomplete = function () { resolve(result && "result" in result ? result.result : result); };
                tx.onerror = function () { reject(tx.error); };
            });
        });
    }

    function get(k) { return store("meta", "readonly", function (s) { return s.get(k); }).then(function (row) { return row ? row.v : null; }); }
    function set(k, v) { return store("meta", "readwrite", function (s) { s.put({ k: k, v: v }); }); }
    function sales() { return store("outbox", "readonly", function (s) { return s.getAll(); }).then(function (rows) { return rows || []; }); }
    function putSale(sale) { return store("outbox", "readwrite", function (s) { s.put(sale); }).then(changed); }
    function removeSale(key) { return store("outbox", "readwrite", function (s) { s.delete(key); }).then(changed); }

    function changed() {
        listeners.forEach(function (fn) { try { fn(); } catch (e) { /* one screen's problem is not another's */ } });
        paintPill();
    }

    // ---------- The snapshot ----------
    function snapshot() { return get("snapshot"); }

    function refresh(url) {
        return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" }).then(function (r) {
            if (unreachable !== false) { unreachable = false; changed(); }
            if (r.redirected || !r.ok || (r.headers.get("content-type") || "").indexOf("json") < 0) throw new Error("not signed in");
            return r.json();
        }, function (err) {
            if (unreachable !== true) { unreachable = true; changed(); }
            throw err;
        }).then(function (snap) {
            return set("snapshot", snap).then(function () { return set("user", snap.user.id); }).then(function () { changed(); return snap; });
        });
    }

    // Kept fresh while the app is open: when it opens (if older than FRESH_MS), every FRESH_MS, and when the connection comes back
    function keepFresh() {
        return snapshot().then(function (snap) {
            const url = me.snapshot || (snap && snap.urls.snapshot);
            if (!url || !navigator.onLine) return snap;
            const age = snap ? Date.now() - Date.parse(snap.at.replace(" ", "T")) : Infinity;
            // While the server cannot be reached it is asked again each time, to notice when it is back; the offline screens always ask
            const screens = document.body && document.body.dataset.offlineApp;
            return screens || unreachable || age > FRESH_MS || !snap || String(snap.user.id) !== String(me.user || snap.user.id) ? refresh(url) : snap;
        }).catch(function () { return null; });
    }

    // ---------- Sales made offline ----------
    function device() {
        try {
            let code = localStorage.getItem("ms-device");
            if (!code) {
                code = Math.random().toString(36).slice(2, 4).toUpperCase();
                localStorage.setItem("ms-device", code);
            }
            const n = Number(localStorage.getItem("ms-device-n") || "0") + 1;
            localStorage.setItem("ms-device-n", String(n));
            return "OFF-" + code + "-" + String(n).padStart(4, "0");
        } catch (e) {
            return "OFF-" + Date.now().toString(36).toUpperCase();
        }
    }

    function newKey() {
        if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
        return Date.now().toString(16) + "-" + Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2);
    }

    // sale: {user, userName, made_at, fields: {customer_name, ...}, cart: [{id, qty, discount}], prices: {id: price}, lines, total}
    function record(sale) {
        sale.key = newKey();
        sale.ref = device();
        sale.status = "waiting";
        return putSale(sale).then(function () {
            // The device's own stock goes down with it, so the next offline sale sees what is left
            return snapshot().then(function (snap) {
                if (!snap) return;
                sale.cart.forEach(function (line) {
                    const p = snap.products.find(function (x) { return x.id === line.id; });
                    if (p) p.quantity -= line.qty;
                });
                return set("snapshot", snap);
            });
        }).then(function () { upload(); return sale; });
    }

    // Each waiting sale of the person signed in, oldest first. Stops at the first one that cannot be sent (no connection, signed out);
    // one the server turns down is kept under "Needs attention" with its reason, and the rest go on.
    function upload() {
        if (uploading) return uploading;
        uploading = snapshot().then(function (snap) {
            if (!snap || !navigator.onLine) return;
            return refresh(snap.urls.snapshot).then(function (fresh) {
                return sales().then(function (all) {
                    const mine = all.filter(function (s) { return s.status === "waiting" && String(s.user) === String(fresh.user.id); })
                        .sort(function (a, b) { return a.made_at < b.made_at ? -1 : 1; });
                    return mine.reduce(function (chain, sale) {
                        return chain.then(function (go) { return go === false ? false : send(fresh, sale); });
                    }, Promise.resolve(true));
                });
            }).then(function (sent) {
                if (sent !== undefined) return refresh(snap.urls.snapshot).catch(function () { /* next time */ });
            });
        }).catch(function () { /* not signed in, or no connection: tried again later */ }).then(function () { uploading = null; changed(); });
        return uploading;
    }

    function send(snap, sale) {
        const form = new FormData();
        form.append("csrf", snap.csrf);
        form.append("upload_key", sale.key);
        form.append("offline_ref", sale.ref);
        form.append("sold_at", sale.made_at);
        form.append("cart", JSON.stringify(sale.cart));
        form.append("prices", JSON.stringify(sale.prices));
        Object.keys(sale.fields).forEach(function (k) { form.append(k, sale.fields[k]); });
        return fetch(snap.urls.sales, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) {
            if (r.redirected) return false;   // signed out, or no longer allowed to sell: kept for later
            return r.json().then(function (answer) {
                if (answer.ok) {
                    sale.status = "uploaded";
                    sale.invoice_no = answer.invoice_no;
                    sale.url = answer.url;
                    sale.uploaded_at = new Date().toISOString();
                } else {
                    sale.status = "attention";
                    sale.problem = answer.error || "The app did not accept it.";
                }
                return putSale(sale).then(function () { return true; });
            });
        }, function () { return false; });   // the connection dropped: tried again later
    }

    // Uploaded sales are shown for a week, then cleared
    function tidy() {
        const week = Date.now() - 7 * 24 * 3600 * 1000;
        return sales().then(function (all) {
            return Promise.all(all.filter(function (s) { return s.status === "uploaded" && Date.parse(s.uploaded_at) < week; }).map(function (s) { return removeSale(s.key); }));
        });
    }

    // ---------- Who is signed in ----------
    // Another person signed in on this device: the snapshot and saved pages were the other person's, so they go
    function checkUser() {
        if (!me.user) return Promise.resolve();
        return get("user").then(function (was) {
            if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({ type: "user", id: me.user });
            if (was !== null && String(was) !== String(me.user)) return wipe();
        });
    }

    function wipe() {
        return Promise.all([
            set("snapshot", null), set("user", null),
            window.caches ? caches.delete(PAGES) : null,
        ]).then(changed);
    }

    // Signing out: the snapshot and saved pages go with the person; their sales not yet uploaded stay, to upload when they sign in again
    document.addEventListener("submit", function (e) {
        const form = e.target;
        if (!(form instanceof HTMLFormElement) || !/\/logout$/.test(new URL(form.action, location.href).pathname) || form.dataset.wiped) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        sales().then(function (all) {
            const waiting = all.filter(function (s) { return s.status !== "uploaded" && String(s.user) === String(me.user); }).length;
            if (waiting && !confirm(waiting + " sale(s) made offline have not uploaded yet. They stay on this device and upload when you sign in again. Sign out?")) return;
            return wipe().then(function () {
                form.dataset.wiped = "1";
                form.submit();
            });
        }).catch(function () { form.dataset.wiped = "1"; form.submit(); });
    }, true);

    // ---------- The pill: what is waiting, and a way to the offline screens ----------
    let pill = null;
    function paintPill() {
        if (document.body && document.body.dataset.offlineApp) return;   // the offline screens show it themselves
        sales().then(function (all) {
            const mine = all.filter(function (s) { return !me.user || String(s.user) === String(me.user); });
            const waiting = mine.filter(function (s) { return s.status === "waiting"; }).length;
            const attention = mine.filter(function (s) { return s.status === "attention"; }).length;
            const offline = !navigator.onLine || unreachable === true;
            const text = attention ? attention + " offline sale(s) need attention" : waiting ? waiting + " sale(s) waiting to upload" : offline ? "No connection: work offline" : "";
            if (!pill) {
                pill = document.createElement("a");
                pill.className = "offline-pill no-print";
                pill.href = "/static/offline.html#waiting";
                pill.setAttribute("data-full-reload", "");
                document.documentElement.appendChild(pill);
            }
            pill.hidden = !text;
            pill.textContent = text;
            pill.classList.toggle("warn", !!attention || offline);
        }).catch(function () { /* this browser keeps nothing: no pill */ });
    }

    window.addEventListener("online", function () { keepFresh().then(upload); paintPill(); });
    window.addEventListener("offline", paintPill);

    window.MSOffline = {
        snapshot: snapshot, sales: sales, record: record, upload: upload, removeSale: removeSale, putSale: putSale,
        reachable: function () { return navigator.onLine && unreachable === false; },
        checking: function () { return navigator.onLine && unreachable === null; },
        refresh: function () { return snapshot().then(function (s) { return s ? refresh(s.urls.snapshot) : null; }); },
        onChange: function (fn) { listeners.push(fn); },
    };

    // The person's look, for the offline screens (which have no server to ask)
    if (!(document.body && document.body.dataset.offlineApp)) {
        try {
            const root = document.documentElement;
            localStorage.setItem("ms-look", JSON.stringify({ theme: root.dataset.theme, accent: root.dataset.accent || "", size: root.dataset.size || "normal",
                glass: Math.round(parseFloat(getComputedStyle(root).getPropertyValue("--glass")) * 100) || 50 }));
        } catch (e) { /* the offline screens use the default look */ }
    }

    if (!window.indexedDB) return;
    checkUser().then(keepFresh).then(function () { upload(); tidy(); paintPill(); });
    setInterval(function () { keepFresh(); upload(); }, FRESH_MS);
    // A page shown from the offline copy: the connection is gone, whatever the browser thinks
    if (document.getElementById("offline-copy")) { unreachable = true; paintPill(); }
    document.addEventListener("nav:swapped", function () { if (document.getElementById("offline-copy") && !unreachable) { unreachable = true; paintPill(); } });
    setInterval(function () { if (unreachable) keepFresh().then(upload); }, 30000);
})();
