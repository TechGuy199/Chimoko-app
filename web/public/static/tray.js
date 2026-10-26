// Who bought it: a product name with data-tray opens, in a panel over the page, the list of the customers who bought
// that product (when, how many, the price and any discount). Loaded once (base.html), so it listens on the document
// and works for the pages nav.js brings in. Ctrl/Cmd-click, or a middle click, still opens the full page in a new tab.
(function () {
    "use strict";

    let tray = null;
    let body = null;
    let asked = 0;

    function build() {
        tray = document.createElement("dialog");
        tray.className = "tray";
        tray.setAttribute("aria-label", "Who bought it");
        tray.innerHTML = '<button type="button" class="tray-close" aria-label="Close">&times;</button><div class="tray-body"></div>';
        body = tray.querySelector(".tray-body");
        tray.querySelector(".tray-close").addEventListener("click", function () { tray.close(); });
        // A click on the dimmed page around the panel closes it
        tray.addEventListener("click", function (e) {
            if (e.target === tray) tray.close();
        });
        // Following a link inside the panel: close it first, so the next page is not hidden under it
        body.addEventListener("click", function (e) {
            const a = e.target.closest("a[href]");
            if (a && !a.hasAttribute("download") && !e.ctrlKey && !e.metaKey && !e.shiftKey) tray.close();
        });
        document.body.appendChild(tray);
    }

    function open(url) {
        if (!tray || !tray.isConnected) build();
        const ask = ++asked;
        body.innerHTML = '<p class="muted tray-loading">Loading who bought it…</p>';
        if (!tray.open) tray.showModal();
        fetch(url, { headers: { "X-Tray": "1" }, credentials: "same-origin" })
            .then(function (r) {
                if (r.redirected || !r.ok) throw new Error("status " + r.status);
                return r.text();
            })
            .then(function (html) {
                if (ask === asked) body.innerHTML = html;
            })
            .catch(function () {
                if (ask !== asked) return;
                body.innerHTML = '<p class="warn">Could not load the sales just now.</p><p><a class="btn small" href="' + url + '">Open as a page</a></p>';
            });
    }

    // Capture phase, so the panel opens before nav.js would treat the click as a page change
    document.addEventListener("click", function (e) {
        const a = e.target.closest("a[data-tray]");
        if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        e.stopPropagation();
        open(a.href);
    }, true);

    // A page change (nav.js) closes the panel
    document.addEventListener("nav:swapped", function () {
        if (tray && tray.open) tray.close();
    });
})();
