// Log a purchase. A box says whether it is also an inventory item (unticked: an expense, logged in Procurements only).
// The category and department are picked as tiles. For an inventory item, after that:
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
    const box = document.getElementById("purchase-inventory");
    const codeHint = document.getElementById("code-hint");
    const submit = document.getElementById("purchase-submit");
    const inventoryOnly = Array.from(form.querySelectorAll("[data-inventory-only]"));
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
        // Unticked, the purchase is only an expense: no stock, unit, selling price or pack size, and the code is optional
        const inventory = box.checked;
        inventoryOnly.forEach(function (label) { label.hidden = !inventory; });
        code.required = inventory;
        codeHint.textContent = inventory ? "" : "(optional)";
        submit.textContent = inventory ? "Log Purchase & Add to Inventory" : "Log Purchase";

        const typedCode = code.value.trim();
        const item = inventory && Object.prototype.hasOwnProperty.call(known, typedCode) ? known[typedCode] : null;

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

        const isSeed = inventory && SEED_WORD.test(chosen(categories));
        row.hidden = !isSeed;
        seeds.required = isSeed;

        if (!inventory) {
            freeUnit();
            unit.value = "";
        } else if (isSeed) {
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

    // Whatever is paid goes to Accounts with how it was paid, so the method shows whenever something is paid. Paid to a named
    // supplier it is also kept on their statement, and then the reference number is needed too. A known supplier fills in their phone.
    const supplier = document.getElementById("purchase-supplier");
    const supplierPhone = document.getElementById("purchase-supplier-phone");
    const payStatus = document.getElementById("purchase-status");
    const payRow = document.getElementById("pay-method-row");
    const payMethod = document.getElementById("purchase-method");
    const optional = document.getElementById("pay-reference-optional");
    function updatePayment() {
        const paying = payStatus.value !== "Unpaid";
        const named = supplier.value.trim() !== "";
        payRow.hidden = !paying;
        payMethod.required = paying;
        payMethod.disabled = !paying;
        payRow.querySelector("[data-reference]").disabled = !paying;
        // payment_method.js reads this to require the reference once a method is picked
        if (named) payMethod.setAttribute("data-require-reference", "");
        else payMethod.removeAttribute("data-require-reference");
        optional.hidden = named;
        payMethod.dispatchEvent(new Event("change"));
    }
    supplier.addEventListener("input", updatePayment);
    payStatus.addEventListener("change", updatePayment);
    supplier.addEventListener("change", function () {
        const typed = supplier.value.trim().toLowerCase();
        const matches = Array.from(document.querySelectorAll("#known-suppliers option")).filter(function (option) {
            return option.value.toLowerCase() === typed;
        });
        if (matches.length === 1 && !supplierPhone.value.trim()) supplierPhone.value = matches[0].dataset.phone || "";
    });
    updatePayment();

    box.addEventListener("change", refresh);
    code.addEventListener("input", refresh);
    categories.concat(departments).forEach(function (radio) { radio.addEventListener("change", refresh); });
    unit.addEventListener("input", function () {
        quantityUnit.textContent = unit.value.trim() ? "(" + unit.value.trim() + ")" : "";
    });
    refresh();

    // A code that matches an item also fills in its name, unless a name has already been typed
    code.addEventListener("change", function () {
        const item = Object.prototype.hasOwnProperty.call(known, code.value.trim()) ? known[code.value.trim()] : null;
        if (box.checked && item && !name.value.trim()) name.value = item.name;
    });
})();
