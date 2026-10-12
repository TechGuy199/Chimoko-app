// Alerts: asks the server every so often for unread alerts, lists them under "Alerts" in the top bar,
// and pops up each new one once. If the browser has been allowed to, it also raises a desktop notification,
// which shows even when this tab is in the background.
(function () {
    const script = document.currentScript;
    const pollUrl = script.dataset.poll;
    const count = document.getElementById("alerts-count");
    const list = document.getElementById("alerts-list");
    const toasts = document.getElementById("toasts");
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

    function poll() {
        fetch(pollUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } })
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (data) {
                if (!data) return;
                count.textContent = data.unread;
                count.hidden = data.unread === 0;
                renderList(data.items);
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

    // Always check once when a page opens; after that, only while the tab is actually being looked at
    poll();
    setInterval(function () { if (!document.hidden) poll(); }, POLL_EVERY);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

    // Settings page: the button that asks the browser for permission to show desktop notifications
    const allow = document.getElementById("allow-desktop-alerts");
    if (allow) {
        const state = document.getElementById("desktop-alerts-state");
        function describe() {
            if (!("Notification" in window)) {
                state.textContent = "This browser cannot show desktop notifications. Popups inside the app still work.";
                allow.disabled = true;
            } else if (Notification.permission === "granted") {
                state.textContent = "Desktop notifications are allowed in this browser.";
                allow.disabled = true;
            } else if (Notification.permission === "denied") {
                state.textContent = "Desktop notifications are blocked for this site. Allow them in the browser's site settings to turn them on.";
                allow.disabled = true;
            } else {
                state.textContent = "Optional: also show alerts as desktop notifications while this site is open in a tab.";
            }
        }
        allow.addEventListener("click", function () {
            Notification.requestPermission().then(describe);
        });
        describe();
    }
})();
