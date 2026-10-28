// Who planted how many trays: a row for each person (the employee and the trays they planted). The trays given to the people add up to the
// trays planted, so once a row has trays the total is worked out from the rows and cannot be typed over. The server checks it all again.
(function () {
    "use strict";

    function setup(box) {
        if (box.dataset.ready) return;
        box.dataset.ready = "1";
        const form = box.closest("form");
        const total = form && form.querySelector("[data-tray-total]");
        const list = box.querySelector("[data-tray-list]");
        const blank = box.querySelector("template[data-tray-blank]");
        const paid = parseFloat(box.dataset.paid) || 0;
        let auto = false;   // the total is showing the sum of the rows, not something that was typed

        function shown(value) {
            return String(Math.round(value * 100) / 100);
        }

        function refresh() {
            if (!total) return;
            let sum = 0;
            let any = false;
            list.querySelectorAll("input[name=planter_trays]").forEach(function (input) {
                const value = parseFloat(input.value);
                if (isFinite(value) && value > 0) { sum += value; any = true; }
            });
            if (any || paid > 0) {
                total.value = shown(paid + sum);
                total.readOnly = true;
                auto = true;
                total.dispatchEvent(new Event("input", { bubbles: true }));
            } else if (auto) {
                total.value = "";
                total.readOnly = false;
                auto = false;
                total.dispatchEvent(new Event("input", { bubbles: true }));
            }
        }

        list.addEventListener("input", refresh);
        box.querySelector("[data-tray-add]").addEventListener("click", function () {
            list.appendChild(blank.content.cloneNode(true));
            const selects = list.querySelectorAll("select");
            selects[selects.length - 1].focus();
        });
        box.addEventListener("click", function (event) {
            const button = event.target.closest("[data-tray-remove]");
            if (!button) return;
            const row = button.closest(".tray-row");
            if (list.querySelectorAll(".tray-row").length > 1) {
                row.remove();
            } else {
                row.querySelector("select").value = "";
                row.querySelector("input").value = "";
            }
            refresh();
        });
        refresh();
    }

    document.querySelectorAll("[data-trays]").forEach(setup);
})();
