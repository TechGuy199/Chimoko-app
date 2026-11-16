// Item page. Three jobs:
//  1. Edit form: the "seeds per pack" box shows when the category is (or becomes) a seed category (seed items only).
//  2. Correct the Quantity: the note label says where the difference went or came from, depending on which way it moves.
//  3. Transfer form: moving seed packs to the POS asks the greenhouse questions and shows what they work out to (seed items only).
(function () {
    // Same rule as the server: the word "seed" or "seeds" in the category ("Seedlings" does not count)
    const SEED_WORD = /\bseeds?\b/i;

    const category = document.getElementById("detail-category");
    const detailRow = document.getElementById("detail-seeds-row");
    const detailSeeds = document.getElementById("detail-seeds");
    const detailUnit = document.getElementById("detail-unit");
    const detailUnitNote = document.getElementById("detail-unit-note");
    // The unit the item had before the category was touched, so switching to Seeds and back loses nothing
    let rememberedUnit = detailUnit && !detailUnit.readOnly ? detailUnit.value : "";
    if (category && detailRow) {
        const showPackSize = function () {
            const isSeed = SEED_WORD.test(category.value);
            detailRow.hidden = !isSeed;
            detailSeeds.required = isSeed;
            // Seeds have their seeds per pack; anything else can say what one pack holds
            const contents = document.getElementById("detail-contents-row");
            if (contents) {
                contents.hidden = isSeed;
                contents.querySelectorAll("input").forEach(function (field) { field.disabled = isSeed; });
            }
            if (detailUnit && isSeed && !detailUnit.value.trim()) detailUnit.value = "pack";
        };
        // The category is a drop-down; "input" and "change" cover every browser
        category.addEventListener("input", showPackSize);
        category.addEventListener("change", showPackSize);
        showPackSize();
    }

    // Correct the Quantity: the note label says where the difference went or came from, depending on which way it moves
    const qtyForm = document.getElementById("quantity-form");
    if (qtyForm) {
        const current = parseInt(qtyForm.dataset.current, 10) || 0;
        const qtyNew = document.getElementById("quantity-new");
        const qtyLabel = document.getElementById("quantity-note-label");
        const qtyNote = document.getElementById("quantity-note");
        const updateQtyLabel = function () {
            const typed = parseInt(qtyNew.value, 10);
            if (!isFinite(typed) || typed === current) {
                qtyLabel.textContent = "Note";
                qtyNote.placeholder = "Say what happened";
            } else if (typed < current) {
                qtyLabel.textContent = "Where did the difference go?";
                qtyNote.placeholder = "e.g. spoilage, damaged, given away, counted wrong before";
            } else {
                qtyLabel.textContent = "Where is the extra stock coming from?";
                qtyNote.placeholder = "e.g. found additional stock, an earlier undercount";
            }
        };
        qtyNew.addEventListener("input", updateQtyLabel);
        updateQtyLabel();
    }

    // Farm use of a measured item can be given as an amount: it shows which packs that opens and what is left over
    (function () {
        const amount = document.getElementById("use-amount");
        const row = document.getElementById("use-amount-row");
        const where = document.getElementById("transfer-destination");
        const whole = document.getElementById("transfer-quantity");
        const note = document.getElementById("use-amount-note");
        if (!amount || !where) return;
        const size = parseFloat(amount.dataset.size) || 0;
        const loose = parseFloat(amount.dataset.loose) || 0;
        const packs = parseInt(amount.dataset.packs, 10) || 0;
        const measure = amount.dataset.measure;
        const words = function (n) { return (Math.round(n * 10000) / 10000).toLocaleString(); };
        function paint() {
            const farm = where.value === "farm";
            row.hidden = !farm;
            if (!farm) amount.value = "";
            const typed = parseFloat(amount.value);
            const byAmount = farm && typed > 0;
            whole.disabled = byAmount;   // the amount decides; whole packs are not asked for
            if (!byAmount) { note.textContent = ""; return; }
            const fromOpen = Math.min(loose, typed);
            const opened = Math.max(0, Math.ceil((typed - fromOpen) / size - 1e-9));
            if (opened > packs) {
                note.textContent = "Only " + words(packs * size + loose) + " " + measure + " is in stock.";
                return;
            }
            const left = loose - fromOpen + opened * size - (typed - fromOpen);
            note.textContent = words(typed) + " " + measure + ": " +
                (fromOpen > 0 ? words(fromOpen) + " from the opened " + amount.dataset.pack + (opened ? ", then " : ". ") : "") +
                (opened ? opened + " " + (opened === 1 ? amount.dataset.pack : amount.dataset.packsWord) + " opened. " : (fromOpen > 0 ? "" : "")) +
                (left > 1e-9 ? words(left) + " " + measure + " left in the opened " + amount.dataset.pack + "." : "Nothing left over.");
        }
        amount.addEventListener("input", paint);
        where.addEventListener("change", paint);
        paint();
    })();

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
