// Overlay editing: a [data-modal-open="id"] button opens the <dialog id="id"> on top of the page you are on, so a
// small edit (rename, edit an account) never takes you away from where you were. Clicking outside the box, the
// Cancel button ([data-modal-close]) or Esc closes it. Saving closes it straight away, while nav.js sends the form
// in the background and brings the updated page in place - the overlay never waits on the server.
// A form marked data-confirm keeps its dialog open if the user declines the confirmation.
// Loaded once, like nav.js. Works from the capture phase so it claims a click before nav.js would follow the link.
(function () {
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
        dialog.close();
    }, true);
})();
