// Settings page: the button that asks the browser for permission to show desktop notifications. Lives on its own
// (moved out of notify.js) because it is specific to this one page's markup and must run again every time this
// page is reached, including by in-place navigation (nav.js), which notify.js itself deliberately does not do.
(function () {
    const allow = document.getElementById("allow-desktop-alerts");
    if (!allow) return;
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
})();
