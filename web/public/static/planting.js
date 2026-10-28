// Record a planting. The trays planted times the holes in each tray give the seeds being planted (unless a number of seeds is typed
// instead). Choosing a seed item from inventory turns the seeds being planted into packs used (and so the stock that leaves
// inventory); the mortality rate turns them into the seedlings to expect.
(function () {
    const form = document.getElementById("planting-form");
    if (!form) return;

    const item = document.getElementById("planting-item");
    const seeds = document.getElementById("planting-seeds");
    const trays = document.getElementById("planting-trays");
    const holes = document.getElementById("planting-holes");
    const traysCalc = document.getElementById("trays-calc");
    const sizeRow = document.getElementById("size-row");
    const sizeInput = document.getElementById("planting-size");
    const manual = document.getElementById("manual-row");
    const nameInput = document.getElementById("planting-name");
    const stockNote = document.getElementById("stock-note");
    const packsNote = document.getElementById("packs-note");
    const ready = document.getElementById("planting-ready");
    const mortality = document.getElementById("planting-mortality");
    const cost = document.getElementById("planting-cost");
    const price = document.getElementById("planting-price");
    const summary = document.getElementById("planting-summary");
    const currency = form.dataset.currency || "";
    const releaseDays = parseInt(form.dataset.release, 10) || 7;
    let info = {};
    try {
        info = JSON.parse(form.dataset.items || "{}");
    } catch (e) { /* the server still checks everything */ }
    let costEdited = false;
    let nameEdited = false;
    let seedsTyped = false;   // the seeds were typed, so the trays no longer decide them

    function number(input) {
        const value = parseFloat(input.value);
        return isFinite(value) ? value : NaN;
    }

    function money(value, places) {
        return currency + value.toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places });
    }

    function current() {
        return item.value && Object.prototype.hasOwnProperty.call(info, item.value) ? info[item.value] : null;
    }

    function packSize(chosen) {
        return chosen ? (chosen.size || parseInt(sizeInput.value, 10) || 0) : 0;
    }

    function minusDays(day, days) {
        const parts = day.split("-").map(Number);
        const date = new Date(parts[0], parts[1] - 1, parts[2] - days);
        const pad = function (n) { return String(n).padStart(2, "0"); };
        return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
    }

    // Trays x holes gives the seeds. A part-planted tray rounds to whole seeds.
    function fromTrays() {
        const t = number(trays);
        const h = number(holes);
        const product = t > 0 && h > 0 ? Math.floor(t * h + 0.5 + 1e-9) : 0;
        if (!seedsTyped) seeds.value = product > 0 ? String(product) : "";
        const typed = parseInt(seeds.value, 10) || 0;
        if (product <= 0) {
            traysCalc.textContent = "";
        } else if (seedsTyped && typed > 0 && typed !== product) {
            traysCalc.textContent = t.toLocaleString() + " trays \u00d7 " + h.toLocaleString() + " holes = " + product.toLocaleString() +
                " seeds, but you typed " + typed.toLocaleString() + ", so " + typed.toLocaleString() + " will be recorded. Clear the seeds to use " + product.toLocaleString() + ".";
        } else {
            traysCalc.textContent = t.toLocaleString() + " trays \u00d7 " + h.toLocaleString() + " holes = " + product.toLocaleString() + " seeds.";
        }
    }

    function refresh() {
        const chosen = current();
        manual.hidden = !!chosen;
        sizeRow.hidden = !(chosen && !chosen.size);
        sizeInput.required = !!(chosen && !chosen.size);
        if (chosen && !nameEdited) nameInput.value = chosen.seedling;
        if (!chosen && !nameEdited) nameInput.value = "";
        stockNote.textContent = chosen
            ? "In stock: " + chosen.packs.toLocaleString() + " pack(s)" + (chosen.size ? " of " + chosen.size.toLocaleString() + " seeds" : "") +
              (chosen.loose ? " and " + chosen.loose.toLocaleString() + " loose seeds" : "") + "."
            : "Nothing is taken out of inventory.";

        const size = packSize(chosen);
        const count = parseInt(seeds.value, 10) || 0;
        packsNote.textContent = "";
        if (chosen && size > 0 && count > 0) {
            const fromLoose = Math.min(chosen.loose, count);
            const need = count - fromLoose;
            const packs = Math.ceil(need / size);
            const left = chosen.loose - fromLoose + packs * size - need;
            let text = count.toLocaleString() + " seeds = " + packs.toLocaleString() + " pack(s) used from inventory";
            if (fromLoose) text += " (after " + fromLoose.toLocaleString() + " loose seeds)";
            if (packs > chosen.packs) {
                text += ", but only " + chosen.packs.toLocaleString() + " are in stock.";
            } else {
                text += ", leaving " + (chosen.packs - packs).toLocaleString() + " pack(s)";
                if (left) text += " and " + left.toLocaleString() + " loose seeds";
                text += ".";
            }
            packsNote.textContent = text;
        }

        if (chosen && size > 0 && !costEdited) cost.value = (chosen.cost / size).toFixed(4);

        const m = number(mortality);
        const seedCost = number(cost);
        if (!(count > 0 && m >= 0 && m < 100)) {
            summary.textContent = "Enter the seeds and the expected mortality to see how many seedlings to expect.";
            return;
        }
        const seedlings = Math.floor(count * (100 - m) / 100 + 1e-9);
        let text = "About " + seedlings.toLocaleString() + " seedlings expected.";
        if (isFinite(seedCost)) {
            const each = seedCost / (1 - m / 100);
            const typed = number(price);
            const sells = isFinite(typed) ? typed : Math.ceil(Math.round(each * 100 * 1e6) / 1e6) / 100;
            text += " Each costs " + money(each, 4) + " to raise; at " + money(sells, 2) + " each they would sell for about " + money(sells * seedlings, 2) + ".";
        }
        if (ready.value) text += " They go on the POS from " + minusDays(ready.value, releaseDays) + ".";
        summary.textContent = text;
    }

    function onTrays() { fromTrays(); refresh(); }
    [trays, holes].forEach(function (input) {
        input.addEventListener("input", onTrays);
        input.addEventListener("change", onTrays);
    });
    seeds.addEventListener("input", function () { seedsTyped = seeds.value.trim() !== ""; fromTrays(); });
    cost.addEventListener("input", function () { costEdited = true; refresh(); });
    nameInput.addEventListener("input", function () { nameEdited = true; });
    item.addEventListener("change", function () { costEdited = false; refresh(); });
    [sizeInput, ready, mortality, price].forEach(function (input) {
        input.addEventListener("input", refresh);
        input.addEventListener("change", refresh);
    });
    seeds.addEventListener("input", refresh);
    seeds.addEventListener("change", refresh);
    fromTrays();
    refresh();
})();
