// Settings page: the button that asks the browser for permission to show desktop notifications. Lives on its own
// (moved out of notify.js) because it is specific to this one page's markup and must run again every time this
// page is reached, including by in-place navigation (nav.js), which notify.js itself deliberately does not do.
(function () {
    const allow = document.getElementById("allow-desktop-alerts");
    if (!allow) return;
    const state = document.getElementById("desktop-alerts-state");

    const push = window.MsPush;
    const test = document.getElementById("push-test");
    const off = document.getElementById("push-off");
    if (push && push.supported) {
        // Push set up on the server: the button turns on notifications that reach this device even with the app closed
        function paint(note) {
            const on = push.on() && Notification.permission === "granted";
            allow.hidden = on;
            allow.disabled = false;
            test.hidden = off.hidden = !on;
            state.textContent = note || (Notification.permission === "denied"
                ? "Notifications are blocked for this site. Allow them in the browser's site settings, then turn them on here."
                : on ? "Notifications are on for this device: alerts reach it even when the app is closed, and the app's icon shows how many are unread."
                : "Turn on notifications to get alerts on this phone or computer even when the app is closed. Do this on each device.");
        }
        allow.addEventListener("click", function () {
            allow.disabled = true;
            push.subscribe().then(function () { paint(); }).catch(function (e) { paint(e.message); });
        });
        test.addEventListener("click", function () {
            test.disabled = true;
            push.post(test.dataset.url, {}).then(function (r) {
                paint(r.ok ? "Test sent: it should appear in a few seconds, even if you close the app first." : r.error);
            }).catch(function () { paint("The test could not be sent. Check the connection."); }).finally(function () { test.disabled = false; });
        });
        off.addEventListener("click", function () {
            push.unsubscribe(off.dataset.url).then(function () { paint("Notifications are off for this device."); });
        });
        paint();
        return;
    }

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
