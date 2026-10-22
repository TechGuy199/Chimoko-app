// Launch screen: "MS-FINTECH - Building custom specialised software for every business". A book opens and a pen writes in it
// (3 seconds), then an infinity loop runs (5 seconds), with tips about the app underneath; the page loads behind it meanwhile.
// It shows every time the app is opened (once per tab or window), not on every page inside it. Skip (or Esc) ends it at once.
// Loaded in the page head, so it can cover the page before anything is painted. Pure CSS and SVG: nothing extra to download.
(function () {
    const KEY = "ms-splash-shown";
    try {
        if (sessionStorage.getItem(KEY)) return;   // already shown since the app was opened in this tab
        sessionStorage.setItem(KEY, "1");
    } catch (e) {
        return;   // storage blocked: better no launch screen than one on every page
    }

    const root = document.documentElement;
    const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const BOOK_MS = calm ? 600 : 3000;
    const TOTAL_MS = calm ? 1500 : 8000;
    const TIPS = [
        "Scan a barcode into the POS search and press Enter: it goes straight into the cart.",
        "Count your cash, bank, Airtel Money and MTN Money once in Accounts; every payment keeps them up to date.",
        "A customer's pre-orders are kept together and collected on one invoice.",
        "Back up at the end of the day: every book lands in a folder on your computer.",
        "Search Planting Records by batch number, or filter by the day a batch is ready.",
        "Log a whole team's work at once with “Everyone on this plot” in Daily Activities.",
        "Lowering a payment asks which account the money goes back to, so balances stay right.",
        "Combine duplicate supplier statements by ticking them and giving them one name.",
        "Every table fits the screen; on a phone, swipe a wide one sideways.",
        "Small edits open on top of the page you are on and close the moment you save.",
    ];
    root.classList.add("splash-on");

    // The infinity: two loops meeting in the middle
    const LOOP = "M100 50 C 125 18, 172 18, 172 50 C 172 82, 125 82, 100 50 C 75 18, 28 18, 28 50 C 28 82, 75 82, 100 50 Z";

    function build() {
        const splash = document.createElement("div");
        splash.className = "splash";
        splash.setAttribute("role", "status");
        splash.setAttribute("aria-label", "Loading MS-FINTECH");
        splash.innerHTML =
            '<button type="button" class="splash-skip">Skip</button>' +
            '<div class="splash-inner">' +
            '<div class="splash-stage" aria-hidden="true">' +
            // The book opening, and the pen writing on the right-hand page
            '<svg class="splash-book" viewBox="0 0 220 150">' +
            '<defs><linearGradient id="splash-page" x1="0" x2="1"><stop offset="0" style="stop-color: var(--surface-alt)"/><stop offset="1" style="stop-color: var(--surface)"/></linearGradient></defs>' +
            '<path class="book-cover" d="M14 34 Q110 22 206 34 L206 132 Q110 120 14 132 Z"/>' +
            '<g class="book-left"><path class="book-page" d="M22 36 Q66 26 110 40 L110 126 Q66 114 22 124 Z"/>' +
            '<path class="book-line" d="M36 56 H96 M36 70 H92 M36 84 H96 M36 98 H82"/></g>' +
            '<g class="book-right"><path class="book-page" d="M110 40 Q154 26 198 36 L198 124 Q154 114 110 126 Z"/>' +
            '<path class="ink ink-1" pathLength="1" d="M122 56 q6 -5 12 0 t12 0 t12 0 t12 0 t12 0"/>' +
            '<path class="ink ink-2" pathLength="1" d="M122 72 q6 -5 12 0 t12 0 t12 0 t12 0"/>' +
            '<path class="ink ink-3" pathLength="1" d="M122 88 q6 -5 12 0 t12 0 t12 0"/></g>' +
            '<path class="book-spine" d="M110 40 L110 126"/>' +
            '<g class="pen"><g transform="rotate(35)">' +
            '<rect class="pen-body" x="-3.2" y="-40" width="6.4" height="30" rx="2"/>' +
            '<rect class="pen-band" x="-3.2" y="-16" width="6.4" height="4"/>' +
            '<path class="pen-nib" d="M-3.2 -12 L3.2 -12 L0 0 Z"/></g></g>' +
            "</svg>" +
            // The infinity loop, with a light running round it
            '<svg class="splash-infinity" viewBox="0 0 200 100">' +
            '<defs><linearGradient id="splash-run" x1="0" x2="1"><stop offset="0" style="stop-color: var(--primary)"/><stop offset="1" style="stop-color: var(--accent)"/></linearGradient></defs>' +
            '<path class="inf-track" d="' + LOOP + '"/>' +
            '<path class="inf-run" pathLength="1" d="' + LOOP + '"/>' +
            '<circle class="inf-dot" r="4.5"><animateMotion dur="1.6s" repeatCount="indefinite" path="' + LOOP + '"/></circle>' +
            "</svg>" +
            "</div>" +
            '<h1 class="splash-brand">MS-FINTECH</h1>' +
            '<p class="splash-tagline">Building custom specialised software for every business</p>' +
            '<div class="splash-bar"><i></i></div>' +
            '<p class="splash-tip" aria-live="polite"><span>Tip</span><em></em></p>' +
            "</div>";
        splash.style.setProperty("--splash-total", TOTAL_MS + "ms");
        document.body.insertBefore(splash, document.body.firstChild);

        // Tips, a new one every two seconds, in a random order
        const tip = splash.querySelector(".splash-tip em");
        const order = TIPS.slice().sort(function () { return Math.random() - 0.5; });
        let shown = 0;
        function nextTip() {
            tip.classList.remove("in");
            setTimeout(function () {
                tip.textContent = order[shown++ % order.length];
                tip.classList.add("in");
            }, shown ? 220 : 0);
        }
        nextTip();
        const tips = setInterval(nextTip, 2000);

        let ended = false;
        function finish() {
            if (ended) return;
            ended = true;
            clearInterval(tips);
            splash.classList.add("done");
            root.classList.remove("splash-on");
            document.removeEventListener("keydown", onKey);
            setTimeout(function () { splash.remove(); }, 550);
        }
        function onKey(e) { if (e.key === "Escape") finish(); }
        splash.querySelector(".splash-skip").addEventListener("click", finish);
        document.addEventListener("keydown", onKey);

        if (calm) splash.classList.add("calm");
        requestAnimationFrame(function () { splash.classList.add("play"); });
        setTimeout(function () { splash.classList.add("loop"); }, BOOK_MS);
        setTimeout(finish, TOTAL_MS);
    }

    if (document.body) build();
    else document.addEventListener("DOMContentLoaded", build);
})();
