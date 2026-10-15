// Overlay editing: a [data-modal-open="id"] button opens the <dialog id="id"> on top of the page you are on, so a
// small edit (rename, edit an account) never takes you away from where you were. Clicking outside the box, the
// Cancel button ([data-modal-close]) or Esc closes it. Saving closes it straight away, while nav.js sends the form
// in the background and brings the updated page in place - the overlay never waits on the server.
// If the server turns the save down (the page comes back with an error message), the same overlay opens again with
// the message inside it and everything that was typed still filled in, ready to be corrected.
// A form marked data-confirm keeps its dialog open if the user declines the confirmation.
// Loaded once, like nav.js. Works from the capture phase so it claims a click before nav.js would follow the link.
(function () {
    const SKIP = /^(hidden|password|file|submit|button|reset|image)$/;
    let pending = null;   // the overlay whose save is on its way to the server

    // What was typed into a form, field by field. Hidden fields (the security token, record ids) and passwords are
    // left to the fresh page, which sends its own.
    function snapshot(form) {
        const values = [];
        Array.from(form.elements).forEach(function (el) {
            if (!el.name || SKIP.test(el.type)) return;
            values.push({ name: el.name, value: el.value, checked: el.checked });
        });
        return values;
    }

    function refill(form, values) {
        values.forEach(function (v) {
            const el = form.elements.namedItem(v.name);
            if (!(el instanceof Element)) return;   // several fields sharing one name: left as the page sent them
            if (el.type === "checkbox" || el.type === "radio") el.checked = v.checked;
            else el.value = v.value;
            // Page scripts (item.js...) keep labels and dependent fields in step with these
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
        });
    }

    document.addEventListener("click", function (e) {
        const opener = e.target.closest("[data-modal-open]");
        if (opener) {
            const dialog = document.getElementById(opener.dataset.modalOpen);
            if (!dialog || !dialog.showModal || e.button !== 0) return;   // no dialog support: the link's own href still works
            e.preventDefault();
            if (!dialog.open) dialog.showModal();
            return;
        }
        const closer = e.target.closest("[data-modal-close]");
        if (closer) {
            const dialog = closer.closest("dialog");
            if (dialog) dialog.close();
            return;
        }
        // The inner box fills the dialog, so a click that lands on the dialog itself is a click on the dark backdrop
        if (e.target instanceof HTMLDialogElement && e.target.open) e.target.close();
    }, true);

    document.addEventListener("submit", function (e) {
        const form = e.target;
        const dialog = form.closest("dialog");
        if (!dialog || form.hasAttribute("data-confirm")) return;
        pending = dialog.id ? {
            id: dialog.id,
            form: Array.from(dialog.querySelectorAll("form")).indexOf(form),
            values: snapshot(form),
            at: Date.now(),
        } : null;
        dialog.close();
    }, true);

    document.addEventListener("nav:swapped", function () {
        const p = pending;
        pending = null;
        if (!p || Date.now() - p.at > 60000) return;
        const main = document.getElementById("page-main");
        const errors = main ? Array.from(main.querySelectorAll(".flash.error")) : [];
        const dialog = document.getElementById(p.id);
        if (!errors.length || !dialog || !dialog.showModal) return;   // saved fine, or the overlay is not on this page

        const form = dialog.querySelectorAll("form")[p.form];
        if (form) refill(form, p.values);
        // The error message moves from the page into the overlay, under its heading
        const body = dialog.querySelector(".modal-body") || dialog;
        let anchor = body.querySelector("h2");
        errors.forEach(function (message) {
            if (anchor) anchor.after(message);
            else body.prepend(message);
            anchor = message;
        });
        dialog.showModal();
    });
})();
