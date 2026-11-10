// Alerts: asks the server every so often for unread alerts, lists them under "Alerts" in the top bar,
// and pops up each new one once. If the browser has been allowed to, it also raises a desktop notification,
// which shows even when this tab is in the background.
// While anything is unread the Alerts button lights up, in the colour of the most pressing alert (red for something wrong, amber for
// something due, the accent colour otherwise), and the installed app's icon carries the count. With notifications turned on for this
// device (Settings > Alerts), alerts also arrive as push notifications while the app is closed (sw.js shows them); window.MsPush
// turns them on and off.
(function () {
    const script = document.currentScript;
    const pollUrl = script.dataset.poll;
    // Looked up fresh each time, not cached: in-place navigation (nav.js) replaces the header and the toasts box
    // with a freshly rendered copy on every page change, and this script runs only once, so it must not hold on
    // to references to DOM nodes that stop existing.
    function countEl() { return document.getElementById("alerts-count"); }
    function listEl() { return document.getElementById("alerts-list"); }
    function toastsEl() { return document.getElementById("toasts"); }
    const POLL_EVERY = 30000;
    const TOAST_FOR = 15000;
    // The colour an alert lights the button with: something wrong, something due, anything else
    const URGENT = /^(low_stock|permission|backup|delete|error)/;
    const DUE = /^(reminders|preorder|collection|delivery|request|payroll|brief)/;

    // ---------- Push notifications for this device ----------
    const pushKey = script.dataset.pushKey || "";
    const pushable = !!pushKey && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    function pushOn() { try { return localStorage.getItem("push.on") === "1"; } catch (e) { return false; } }
    function setPushOn(on) { try { if (on) localStorage.setItem("push.on", "1"); else localStorage.removeItem("push.on"); } catch (e) { /* fine */ } }
    function keyBytes(base64) {
        const padded = (base64 + "===".slice((base64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
        return Uint8Array.from(atob(padded), function (c) { return c.charCodeAt(0); });
    }
    function post(url, fields) {
        return fetch(url, { method: "POST", credentials: "same-origin", body: new URLSearchParams(Object.assign({ csrf: script.dataset.csrf }, fields)) })
            .then(function (r) { return r.json(); });
    }
    function tell(subscription) {
        const json = subscription.toJSON();
        return post(script.dataset.subscribe, { endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth, device: navigator.userAgent.slice(0, 120) });
    }
    // Must run from a click: the browser asks the person, then gives this device a push address, which the server keeps
    async function subscribe() {
        if (!pushable) throw new Error("This browser cannot receive push notifications. On an iPhone, add the app to the Home Screen first.");
        if (await Notification.requestPermission() !== "granted") throw new Error("Notifications were not allowed. Allow them for this site in the browser's settings.");
        const worker = await navigator.serviceWorker.ready;
        let subscription = await worker.pushManager.getSubscription();
        const key = keyBytes(pushKey);
        if (subscription && subscription.options && subscription.options.applicationServerKey) {
            const old = new Uint8Array(subscription.options.applicationServerKey);
            if (old.length !== key.length || old.some(function (b, i) { return b !== key[i]; })) { await subscription.unsubscribe(); subscription = null; }
        }
        if (!subscription) subscription = await worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
        const answer = await tell(subscription);
        if (!answer.ok) throw new Error(answer.error || "Notifications could not be turned on.");
        setPushOn(true);
        return answer;
    }
    async function unsubscribe(url) {
        setPushOn(false);
        if (!("serviceWorker" in navigator)) return;
        const worker = await navigator.serviceWorker.ready;
        const subscription = await worker.pushManager.getSubscription();
        if (!subscription) return;
        await post(url, { endpoint: subscription.endpoint });
        await subscription.unsubscribe();
    }
    window.MsPush = { supported: pushable, on: pushOn, subscribe: subscribe, unsubscribe: unsubscribe, post: post };
    // Turned on before: tell the server again quietly (whoever is signed in now gets this device's notifications; a renewed address is kept)
    if (pushable && pushOn() && Notification.permission === "granted") {
        navigator.serviceWorker.ready.then(function (w) { return w.pushManager.getSubscription(); })
            .then(function (s) { if (s) return tell(s); setPushOn(false); }).catch(function () { /* next time */ });
    }

    // Alerts already popped up in this browser session, so moving between pages does not repeat them
    let shown;
    try {
        shown = new Set(JSON.parse(sessionStorage.getItem("alertsShown") || "[]"));
    } catch (e) {
        shown = new Set();
    }
    function remember(id) {
        shown.add(id);
        try {
            sessionStorage.setItem("alertsShown", JSON.stringify(Array.from(shown).slice(-200)));
        } catch (e) { /* private browsing: popups may repeat, nothing else breaks */ }
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;   // textContent: alert text is never treated as HTML
        return node;
    }

    function renderList(items) {
        const list = listEl();
        if (!list) return;   // the header is momentarily out of the document mid-navigation
        list.replaceChildren();
        if (!items.length) {
            list.appendChild(element("span", "menu-note", "Nothing new."));
            return;
        }
        items.forEach(function (item) {
            const link = element("a", "menu-item alert-item");
            link.href = item.open;
            link.appendChild(element("strong", "", item.title));
            link.appendChild(element("span", "", item.body));
            link.appendChild(element("small", "", item.created_at));
            list.appendChild(link);
        });
    }

    function toast(item) {
        const toasts = toastsEl();
        if (!toasts) return;
        const box = element("div", "toast");
        box.setAttribute("role", "alert");
        const close = element("button", "toast-close", "×");
        close.type = "button";
        close.setAttribute("aria-label", "Dismiss");
        const open = element("a", "btn small primary", "Open");
        open.href = item.open;
        box.appendChild(close);
        box.appendChild(element("strong", "", item.title));
        box.appendChild(element("p", "", item.body));
        box.appendChild(open);
        function dismiss() {
            box.classList.add("leaving");
            setTimeout(function () { box.remove(); }, 250);
        }
        close.addEventListener("click", dismiss);
        setTimeout(dismiss, TOAST_FOR);
        toasts.appendChild(box);

        // With push on, the device shows the alert itself (sw.js), so it is not shown twice
        if ("Notification" in window && Notification.permission === "granted" && !pushOn()) {
            try {
                const desktop = new Notification(item.title, { body: item.body, tag: "alert-" + item.id });
                desktop.onclick = function () { window.focus(); window.location.href = item.open; };
            } catch (e) { /* some mobile browsers only allow this from a service worker */ }
        }
    }

    // The last answer, drawn again into the fresh header that every in-place page change brings (it arrives from the server
    // with no alerts in it), so the count does not vanish until the next round
    let last = null;
    let lastAt = 0;
    function draw(data) {
        const count = countEl();
        if (count) {
            count.textContent = data.unread;
            count.hidden = data.unread === 0;
            const button = count.closest("summary") || count.parentElement;
            const kinds = data.items.map(function (i) { return i.kind || ""; });
            const tone = kinds.some(function (k) { return URGENT.test(k); }) ? "urgent" : kinds.some(function (k) { return DUE.test(k); }) ? "due" : "news";
            button.classList.toggle("alerts-lit", data.unread > 0);
            if (data.unread > 0) button.dataset.tone = tone; else delete button.dataset.tone;
            button.title = data.unread ? data.unread + " unread alert" + (data.unread === 1 ? "" : "s") : "No unread alerts";
        }
        // The installed app's icon (taskbar, dock, home screen) shows the count where the device supports it
        try {
            if (navigator.setAppBadge) { if (data.unread) navigator.setAppBadge(data.unread); else navigator.clearAppBadge(); }
        } catch (e) { /* not supported here */ }
        renderList(data.items);
    }
    document.addEventListener("nav:swapped", function () {
        if (last) draw(last);
        if (Date.now() - lastAt > 10000) poll();   // moving around the app keeps the alerts current too
    });

    function poll() {
        lastAt = Date.now();
        fetch(pollUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } })
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (data) {
                if (!data) return;
                last = data;
                draw(data);
                // Oldest first, so a burst of alerts stacks in the order it happened
                data.items.slice().reverse().forEach(function (item) {
                    if (!shown.has(item.id)) {
                        remember(item.id);
                        toast(item);
                    }
                });
            })
            .catch(function () { /* offline or signed out: try again on the next round */ });
    }

    // Always check once when a page opens; after that, only while the tab is actually being looked at. This script
    // tag is loaded once and deliberately never re-run by in-place navigation (nav.js), so this interval keeps
    // running for as long as the tab is open, regardless of how many pages are visited inside it.
    poll();
    setInterval(function () { if (!document.hidden) poll(); }, POLL_EVERY);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
})();
