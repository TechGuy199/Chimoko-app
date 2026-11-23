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

    // Picking a product from inventory fills in what it is measured in (kg, litres) or counted in (bag), and says it comes out of stock
    function matchOf(row) {
        const typed = row.querySelector("[data-product]").value.trim().toLowerCase();
        return Array.from(document.querySelectorAll("#activity-items option")).find(function (o) { return o.value.toLowerCase() === typed; });
    }
    function paintStock(row) {
        const note = row.querySelector("[data-stock-note]");
        if (!note) return;
        const match = matchOf(row);
        const qty = parseFloat(row.querySelector('[name="product_qty"]').value);
        note.hidden = !match;
        if (!match) return;
        const unit = row.querySelector("[data-unit]").value.trim() || match.dataset.unit || match.dataset.pack;
        note.textContent = (qty > 0 ? qty.toLocaleString() + " " + unit + " will come out of inventory. " : "From inventory: give the quantity used and it comes out of stock. ") +
            match.dataset.stock + "." + (match.dataset.measure ? " Give it in " + match.dataset.measure + ", or in whole " + match.dataset.pack + "s." : "");
    }
    rows.addEventListener("change", function (e) {
        if (!e.target.matches("[data-product]")) return;
        const row = e.target.closest(".activity-row");
        const unit = row.querySelector("[data-unit]");
        const match = matchOf(row);
        if (match && match.dataset.unit && !unit.value) unit.value = match.dataset.unit;
        paintStock(row);
    });
    rows.addEventListener("input", function (e) {
        if (e.target.matches('[data-product], [name="product_qty"], [data-unit]')) paintStock(e.target.closest(".activity-row"));
    });

    renumber();
})();
