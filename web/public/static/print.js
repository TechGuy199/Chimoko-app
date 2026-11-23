// Printing a receipt or invoice: on a regular printer (A4) or on a receipt (thermal) printer with an 80 mm or 58 mm roll.
// The printer belongs to the device, so the choice is remembered on it (asked the first time, changeable any time). A receipt printer
// gets the page's narrow receipt ([data-thermal]) on a page exactly as long as it, so the printer feeds and cuts at its end; A4 gets
// the full invoice. Any button with data-print prints (data-print="choose" always asks first). Loaded once for the app and the
// offline screens; it works on whichever page is showing.
(function () {
    "use strict";
    if (window.MSPrint) return;
    const KEY = "ms-printer";
    const PRINTERS = [
        { key: "a4", label: "Regular printer", detail: "A4 paper, the full invoice" },
        { key: "80", label: "Receipt printer, 80 mm", detail: "The usual till-roll width" },
        { key: "58", label: "Receipt printer, 58 mm", detail: "Small and handheld printers" },
    ];
    const WIDTH = { "80": 80, "58": 58 };   // paper width; the receipt itself is a little narrower (the printer's margins)

    function saved() {
        try { return localStorage.getItem(KEY); } catch (e) { return null; }
    }

    function remember(key) {
        try { localStorage.setItem(KEY, key); } catch (e) { /* asked again next time */ }
        paintLabels();
    }

    function nameOf(key) {
        const p = PRINTERS.find(function (x) { return x.key === key; });
        return p ? p.label : "";
    }

    function pageRule(css) {
        let style = document.getElementById("print-page");
        if (!style) {
            style = document.createElement("style");
            style.id = "print-page";
            document.head.appendChild(style);
        }
        style.textContent = css;
    }

    function reset() {
        document.documentElement.classList.remove("print-thermal", "print-thermal-80", "print-thermal-58", "print-a4");
        pageRule("");
    }

    function print(key) {
        reset();
        const root = document.documentElement;
        const receipt = document.querySelector("[data-thermal]");
        if (WIDTH[key] && receipt) {
            root.classList.add("print-thermal", "print-thermal-" + key);
            // Measured at the roll's width, so the page is as long as the receipt (plus a little to tear off)
            receipt.classList.add("thermal-measuring");
            const tall = receipt.getBoundingClientRect().height;
            receipt.classList.remove("thermal-measuring");
            const length = Math.ceil(tall * 25.4 / 96) + 10;
            pageRule("@page { size: " + WIDTH[key] + "mm " + length + "mm; margin: 0; }");
        } else {
            root.classList.add("print-a4");
            pageRule("@page { size: A4; margin: 14mm; }");
        }
        window.print();
    }

    window.addEventListener("afterprint", reset);

    // ---------- Which printer? ----------
    function choose(then) {
        const old = document.getElementById("print-choose");
        if (old) old.remove();
        const dialog = document.createElement("dialog");
        dialog.id = "print-choose";
        dialog.className = "modal print-choose";
        const body = document.createElement("div");
        body.className = "modal-body";
        const title = document.createElement("h2");
        title.textContent = "Which printer?";
        body.appendChild(title);
        const hint = document.createElement("p");
        hint.className = "muted";
        hint.textContent = "Remembered on this device, so you are not asked every time. Change it from Print options.";
        body.appendChild(hint);
        const current = saved();
        PRINTERS.forEach(function (p) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "print-option" + (p.key === current ? " current" : "");
            const strong = document.createElement("strong");
            strong.textContent = p.label;
            const small = document.createElement("small");
            small.textContent = p.detail;
            button.appendChild(strong);
            button.appendChild(small);
            button.addEventListener("click", function () {
                remember(p.key);
                dialog.close();
                dialog.remove();
                if (then) then(p.key);
            });
            body.appendChild(button);
        });
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn ghost wide";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", function () { dialog.close(); dialog.remove(); });
        body.appendChild(cancel);
        dialog.appendChild(body);
        document.body.appendChild(dialog);
        dialog.showModal();
    }

    // Each [data-print-label] says which printer this device prints on
    function paintLabels() {
        const key = saved();
        document.querySelectorAll("[data-print-label]").forEach(function (el) {
            el.textContent = key ? "Printing on: " + nameOf(key) : "You are asked which printer the first time";
        });
    }

    document.addEventListener("click", function (e) {
        const button = e.target.closest("[data-print]");
        if (!button) return;
        e.preventDefault();
        const menu = button.closest("details");
        if (menu) menu.open = false;
        if (button.dataset.print === "choose" || !saved()) choose(print);
        else print(saved());
    });
    document.addEventListener("nav:swapped", paintLabels);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", paintLabels); else paintLabels();

    window.MSPrint = { print: print, choose: choose, saved: saved };
})();
