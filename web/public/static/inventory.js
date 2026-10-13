// Purchase form: a seed item (picked by its category, or by an existing item code) must say how many
// seeds are in one pack, and stock of it is then counted in packs.
(function () {
    const form = document.getElementById("purchase-form");
    if (!form) return;

    const code = document.getElementById("purchase-code");
    const category = document.getElementById("purchase-category");
    const row = document.getElementById("seeds-row");
    const seeds = document.getElementById("purchase-seeds");
    const note = document.getElementById("seeds-note");
    const unit = document.getElementById("quantity-unit");
    let known = {};
    try {
        known = JSON.parse(form.dataset.seedItems || "{}");
    } catch (e) { /* the server still checks everything */ }

    // Same rule as the server: the word "seed" or "seeds" in the category ("Seedlings" does not count)
    const SEED_WORD = /\bseeds?\b/i;

    function refresh() {
        const typedCode = code.value.trim();
        const stored = Object.prototype.hasOwnProperty.call(known, typedCode) ? known[typedCode] : null;
        const isSeed = stored !== null || SEED_WORD.test(category.value);

        row.hidden = !isSeed;
        seeds.required = isSeed;
        unit.textContent = isSeed ? "(packs)" : "";

        if (stored) {
            // Already in stock with a pack size: stock is counted in these packs, so the size is fixed
            seeds.value = stored;
            seeds.readOnly = true;
            note.textContent = "This item is counted in packs of " + Number(stored).toLocaleString() + " seeds. Use a new item code for a different pack size.";
        } else {
            if (seeds.readOnly) seeds.value = "";
            seeds.readOnly = false;
            note.textContent = "Quantity bought below is counted in packs.";
        }
    }

    code.addEventListener("input", refresh);
    category.addEventListener("input", refresh);
    refresh();
})();
