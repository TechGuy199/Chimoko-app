// Log a purchase. The category and department are picked first, as tiles. After that:
//  - a seed category asks how many seeds are in one pack (stock is then counted in packs, and the unit is "pack");
//  - an item code that is already in stock shows its own category, department and unit, and the purchase adds to it.
(function () {
    const form = document.getElementById("purchase-form");
    if (!form) return;

    const code = document.getElementById("purchase-code");
    const name = document.getElementById("purchase-name");
    const row = document.getElementById("seeds-row");
    const seeds = document.getElementById("purchase-seeds");
    const note = document.getElementById("seeds-note");
    const unit = document.getElementById("purchase-unit");
    const unitNote = document.getElementById("unit-note");
    const quantityUnit = document.getElementById("quantity-unit");
    const restock = document.getElementById("restock-note");
    const categories = Array.from(form.querySelectorAll('input[name="category"]'));
    const departments = Array.from(form.querySelectorAll('input[name="department"]'));

    let known = {};
    try {
        known = JSON.parse(form.dataset.items || "{}");
    } catch (e) { /* the server still checks everything */ }

    // Same rule as the server: the word "seed" or "seeds" in the category name ("Seedlings" does not count)
    const SEED_WORD = /\bseeds?\b/i;
    let unitWasForced = false;   // true while the unit box shows a value this script put there

    function choose(radios, value) {
        radios.forEach(function (radio) { radio.checked = radio.value === value; });
    }

    function chosen(radios) {
        const picked = radios.find(function (radio) { return radio.checked; });
        return picked ? picked.value : "";
    }

    function lockUnit(value, message) {
        unit.value = value;
        unit.readOnly = true;
        unitNote.textContent = message;
        unitWasForced = true;
    }

    function freeUnit() {
        if (unitWasForced) unit.value = "";
        unit.readOnly = false;
        unitNote.textContent = "";
        unitWasForced = false;
    }

    function refresh() {
        const typedCode = code.value.trim();
        const item = Object.prototype.hasOwnProperty.call(known, typedCode) ? known[typedCode] : null;

        if (item) {
            // An item already in stock keeps its own category and department
            choose(categories, item.category);
            choose(departments, item.department);
            restock.hidden = false;
            restock.textContent = "This code is already in stock as “" + item.name + "” under " + item.category + " / " +
                item.department + ". The purchase adds to it and keeps that category and department.";
        } else {
            restock.hidden = true;
        }

        const isSeed = SEED_WORD.test(chosen(categories));
        row.hidden = !isSeed;
        seeds.required = isSeed;

        if (isSeed) {
            lockUnit("pack", "Seed items are counted in packs.");
        } else if (item && item.unit) {
            lockUnit(item.unit, "This item is counted in " + item.unit + ".");
        } else {
            freeUnit();
        }
        quantityUnit.textContent = unit.value.trim() ? "(" + unit.value.trim() + ")" : "";

        if (item && item.seeds) {
            // Already counted in packs of this size, so the size is fixed
            seeds.value = item.seeds;
            seeds.readOnly = true;
            note.textContent = "This item is counted in packs of " + Number(item.seeds).toLocaleString() + " seeds. Use a new item code for a different pack size.";
        } else {
            if (seeds.readOnly) seeds.value = "";
            seeds.readOnly = false;
            note.textContent = "Quantity bought below is counted in packs.";
        }
    }

    code.addEventListener("input", refresh);
    categories.concat(departments).forEach(function (radio) { radio.addEventListener("change", refresh); });
    unit.addEventListener("input", function () {
        quantityUnit.textContent = unit.value.trim() ? "(" + unit.value.trim() + ")" : "";
    });
    refresh();

    // A code that matches an item also fills in its name, unless a name has already been typed
    code.addEventListener("change", function () {
        const item = Object.prototype.hasOwnProperty.call(known, code.value.trim()) ? known[code.value.trim()] : null;
        if (item && !name.value.trim()) name.value = item.name;
    });
})();
