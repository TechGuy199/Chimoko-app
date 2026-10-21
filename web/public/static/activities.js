// Logging a plot's activities: add or remove activity rows (each saved together), and fill in a product's unit from inventory.
(function () {
    const rows = document.getElementById("activity-rows");
    const template = document.getElementById("activity-row-template");
    const add = document.getElementById("add-activity");
    if (!rows || !template || !add) return;

    function renumber() {
        const all = rows.querySelectorAll(".activity-row");
        all.forEach(function (row, i) {
            row.querySelector("[data-row-number]").textContent = i + 1;
            row.querySelector("[data-remove-row]").hidden = all.length === 1;   // there is always at least one row
        });
    }

    add.addEventListener("click", function () {
        const last = rows.querySelector(".activity-row:last-of-type");
        const row = template.content.firstElementChild.cloneNode(true);
        // A new row starts with the same worker and time as the one before it, which is usually what comes next
        if (last) {
            row.querySelector('[name="worker"]').value = last.querySelector('[name="worker"]').value;
            row.querySelector('[name="period"]').value = last.querySelector('[name="period"]').value;
        }
        rows.appendChild(row);
        renumber();
        row.querySelector('[name="description"]').focus();
    });

    rows.addEventListener("click", function (e) {
        const remove = e.target.closest("[data-remove-row]");
        if (!remove) return;
        remove.closest(".activity-row").remove();
        renumber();
    });

    // Picking a product from inventory fills in the unit it is counted in, unless one was typed
    rows.addEventListener("change", function (e) {
        if (!e.target.matches("[data-product]")) return;
        const unit = e.target.closest(".activity-row").querySelector("[data-unit]");
        const match = Array.from(document.querySelectorAll("#activity-items option")).find(function (o) { return o.value === e.target.value; });
        if (match && match.dataset.unit && !unit.value) unit.value = match.dataset.unit;
    });

    renumber();
})();
