// Item page. Two jobs, both for seed items:
//  1. Edit form: the "seeds per pack" box shows when the category is (or becomes) a seed category.
//  2. Transfer form: moving seed packs to the POS asks the greenhouse questions and shows what they work out to.
(function () {
    // Same rule as the server: the word "seed" or "seeds" in the category ("Seedlings" does not count)
    const SEED_WORD = /\bseeds?\b/i;

    const category = document.getElementById("detail-category");
    const detailRow = document.getElementById("detail-seeds-row");
    const detailSeeds = document.getElementById("detail-seeds");
    if (category && detailRow) {
        const showPackSize = function () {
            const isSeed = SEED_WORD.test(category.value);
            detailRow.hidden = !isSeed;
            detailSeeds.required = isSeed;
        };
        category.addEventListener("input", showPackSize);
        showPackSize();
    }

    const form = document.getElementById("transfer-form");
    if (!form || form.dataset.seed !== "1") return;

    const destination = document.getElementById("transfer-destination");
    const quantity = document.getElementById("transfer-quantity");
    const fields = document.getElementById("seedling-fields");
    const packSize = document.getElementById("sf-seeds");                 // only present for items with no recorded pack size
    const germination = document.getElementById("sf-germination");
    const seedCost = document.getElementById("sf-seed-cost");
    const price = document.getElementById("sf-price");
    const summary = document.getElementById("sf-summary");
    const currency = form.dataset.currency || "";
    const packCost = parseFloat(form.dataset.packCost) || 0;
    const knownSize = parseInt(form.dataset.seedsPerPack, 10) || 0;
    let seedCostEdited = false;

    function number(input) {
        const value = parseFloat(input.value);
        return isFinite(value) ? value : NaN;
    }

    function money(value, places) {
        return currency + value.toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places });
    }

    function seedsInPack() {
        return knownSize || (packSize ? parseInt(packSize.value, 10) || 0 : 0);
    }

    function roundUpToCent(value) {
        return Math.ceil(Math.round(value * 100 * 1e6) / 1e6) / 100;   // matches the server's rounding
    }

    function refresh() {
        const selling = destination.value === "pos";
        fields.disabled = !selling;
        fields.hidden = !selling;
        germination.required = selling;
        seedCost.required = selling;
        if (packSize) packSize.required = selling;
        if (!selling) return;

        // Until the cost of one seed is typed over, it follows the pack cost and the pack size
        const size = seedsInPack();
        if (!seedCostEdited && size > 0) seedCost.value = (packCost / size).toFixed(4);

        const packs = parseInt(quantity.value, 10) || 0;
        const rate = number(germination);
        const cost = number(seedCost);
        if (!(size > 0 && packs > 0 && rate > 0 && rate <= 100 && cost >= 0)) {
            summary.textContent = "Enter the germination rate to see how many seedlings to expect.";
            return;
        }
        const seeds = packs * size;
        const seedlings = Math.floor(seeds * rate / 100 + 1e-9);
        const perSeedling = cost / (rate / 100);
        const typedPrice = number(price);
        const sells = isFinite(typedPrice) ? typedPrice : roundUpToCent(perSeedling);
        summary.textContent =
            packs.toLocaleString() + " pack(s) = " + seeds.toLocaleString() + " seeds, about " + seedlings.toLocaleString() +
            " seedlings. Each costs " + money(perSeedling, 4) + " to raise; at " + money(sells, 2) + " each they would sell for about " +
            money(sells * seedlings, 2) + ".";
    }

    seedCost.addEventListener("input", function () { seedCostEdited = true; refresh(); });
    [destination, quantity, germination, price].forEach(function (input) {
        input.addEventListener("input", refresh);
        input.addEventListener("change", refresh);
    });
    if (packSize) packSize.addEventListener("input", refresh);
    refresh();
})();
