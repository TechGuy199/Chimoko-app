// Alerts: asks the server every so often for unread alerts, lists them under "Alerts" in the top bar,
// and pops up each new one once. If the browser has been allowed to, it also raises a desktop notification,
// which shows even when this tab is in the background.
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

        if ("Notification" in window && Notification.permission === "granted") {
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
        }
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
