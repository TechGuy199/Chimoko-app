// A planter's day: add or remove entries (all saved together). Choosing a seed item fills in the mortality and ready date from its
// latest planting, or, when a batch of it was already planted that day in trays with the same holes, shows that the entry joins that
// batch (which keeps its own mortality and ready date). Trays x holes gives the seeds, shown against the stock.
(function () {
    const form = document.getElementById("planter-form");
    const rows = document.getElementById("entry-rows");
    const template = document.getElementById("entry-row-template");
    const add = document.getElementById("add-entry");
    if (!form || !rows || !template || !add) return;

    let info = {};
    try {
        info = JSON.parse(form.dataset.items || "{}");
    } catch (e) { /* the server still checks everything */ }
    const day = form.dataset.day;

    function plusDays(start, days) {
        const parts = start.split("-").map(Number);
        const date = new Date(parts[0], parts[1] - 1, parts[2] + days);
        const pad = function (n) { return String(n).padStart(2, "0"); };
        return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
    }

    // Batches this form would start: a second entry for the same item and holes joins the first
    function startedAbove(row, itemId, holes) {
        for (const other of rows.querySelectorAll("[data-entry]")) {
            if (other === row) return false;
            if (other.querySelector("[data-entry-item]").value === itemId && other.querySelector("[data-entry-holes]").value === holes) return true;
        }
        return false;
    }

    function paint(row, itemChanged) {
        const itemId = row.querySelector("[data-entry-item]").value;
        const chosen = itemId && Object.prototype.hasOwnProperty.call(info, itemId) ? info[itemId] : null;
        const trays = parseFloat(row.querySelector("[data-entry-trays]").value);
        const holesInput = row.querySelector("[data-entry-holes]");
        const holes = parseInt(holesInput.value, 10);
        const mortality = row.querySelector("[data-entry-mortality]");
        const ready = row.querySelector("[data-entry-ready]");
        const note = row.querySelector("[data-entry-note]");
        const batch = chosen ? (chosen.batches || {})[String(holes || 0)] : null;
        const joins = !!batch || (chosen && startedAbove(row, itemId, holesInput.value));

        // Joining a batch: its own mortality and ready date stand, so these are not asked for
        // (read-only, not disabled: every entry must still send each field, so the rows line up on the server)
        mortality.readOnly = ready.readOnly = !!joins;
        mortality.required = ready.required = !joins;
        if (batch) {
            mortality.value = batch.mortality;
            ready.value = batch.ready || "";
        } else if (chosen && itemChanged && !joins) {
            if (chosen.mortality !== null && chosen.mortality !== undefined) mortality.value = chosen.mortality;
            if (chosen.grow !== null && chosen.grow !== undefined && day) ready.value = plusDays(day, chosen.grow);
        }

        const parts = [];
        // What these trays earn, by their kind (casual workers only)
        const kind = row.querySelector("[data-entry-kind]");
        const picked = kind && kind.selectedOptions[0];
        if (form.dataset.paid === "1" && picked && picked.value && trays > 0) {
            const rate = parseFloat(picked.dataset.rate) || 0;
            const money = function (n) { return (form.dataset.currency || "") + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
            parts.push(trays.toLocaleString() + " " + picked.textContent.split(" \u00b7 ")[0].trim() + " tray" + (trays === 1 ? "" : "s") + " \u00d7 " + money(rate) + " = " + money(trays * rate) + " pay.");
        }
        if (chosen && trays > 0 && holes > 0) {
            const seeds = Math.floor(trays * holes + 0.5 + 1e-9);
            parts.push(trays.toLocaleString() + (trays === 1 ? " tray × " : " trays × ") + holes.toLocaleString() + " holes = " + seeds.toLocaleString() + " seeds.");
            const inStock = chosen.packs * chosen.size + chosen.loose;
            if (chosen.size && seeds > inStock) {
                parts.push("Only " + inStock.toLocaleString() + " seeds are in stock.");
            } else if (chosen.size) {
                const packs = Math.max(0, Math.ceil((seeds - Math.min(chosen.loose, seeds)) / chosen.size));
                parts.push(packs ? "Uses " + packs + " pack" + (packs === 1 ? "" : "s") + " of " + chosen.packs + "." : "Uses loose seeds only.");
            }
        }
        if (batch) parts.push("Added to " + batch.label + ", already planted this day.");
        else if (joins) parts.push("Added to the batch started by an entry above.");
        else if (chosen && chosen.mortality === null) parts.push("First planting of this item: enter its mortality and ready date.");
        note.textContent = parts.join(" ");
    }

    function paintAll() {
        rows.querySelectorAll("[data-entry]").forEach(function (row) { paint(row, false); });
    }

    function renumber() {
        const all = rows.querySelectorAll("[data-entry]");
        all.forEach(function (row, i) {
            row.querySelector("[data-row-number]").textContent = i + 1;
            row.querySelector("[data-remove-row]").hidden = all.length === 1;   // there is always at least one entry
        });
    }

    add.addEventListener("click", function () {
        const last = rows.querySelector("[data-entry]:last-of-type");
        const row = template.content.firstElementChild.cloneNode(true);
        // A new entry starts with the holes of the one before it: usually the same trays
        if (last) row.querySelector("[data-entry-holes]").value = last.querySelector("[data-entry-holes]").value;
        // and the same kind of tray
        if (last && last.querySelector("[data-entry-kind]")) row.querySelector("[data-entry-kind]").value = last.querySelector("[data-entry-kind]").value;
        rows.appendChild(row);
        renumber();
        paintAll();
        row.querySelector("[data-entry-item]").focus();
    });

    rows.addEventListener("click", function (e) {
        const remove = e.target.closest("[data-remove-row]");
        if (!remove) return;
        remove.closest("[data-entry]").remove();
        renumber();
        paintAll();
    });

    rows.addEventListener("input", function (e) {
        const row = e.target.closest("[data-entry]");
        if (!row) return;
        paint(row, e.target.matches("[data-entry-item]"));
        if (e.target.matches("[data-entry-item], [data-entry-holes]")) paintAll();
    });
    // Choosing the kind of tray fills in how many holes it has
    rows.addEventListener("change", function (e) {
        if (!e.target.matches("[data-entry-kind]")) return;
        const row = e.target.closest("[data-entry]");
        const holes = e.target.selectedOptions[0] && e.target.selectedOptions[0].dataset.holes;
        if (holes) row.querySelector("[data-entry-holes]").value = holes;
        paintAll();
    });
    rows.addEventListener("change", function (e) {
        const row = e.target.closest("[data-entry]");
        if (row && e.target.matches("[data-entry-item]")) {
            paint(row, true);
            paintAll();
        }
    });

    renumber();
    paintAll();
})();
