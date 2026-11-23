// Scan a receipt. Two pages share this file:
//  - the scan page: a photo is taken (or a photo or PDF chosen), shrunk on the device so it is quick to send, and sent, to be read by
//    Gemini or typed in by hand; the review page opens next;
//  - the review page: the receipt's lines, each an inventory item, a new item or an expense. New items ask where they are filed; stock
//    asks what it is for. The lines' total is checked against the receipt's printed total, and the payment fields follow the status.
// The server checks everything again; nothing here decides what is saved.
(function () {
    "use strict";

    // ---------- The scan page ----------
    const scan = document.getElementById("receipt-scan");
    if (scan) {
        const MAX_SIDE = 2000;   // enough to read small print, a few hundred kB as JPEG
        const pick = document.getElementById("receipt-pick");
        const chosen = document.getElementById("receipt-chosen");
        const preview = document.getElementById("receipt-preview");
        const error = document.getElementById("receipt-error");
        const status = document.getElementById("receipt-status");
        const ways = document.getElementById("receipt-ways");
        let file = null;

        function shrink(original) {
            return new Promise(function (resolve) {
                const url = URL.createObjectURL(original);
                const img = new Image();
                img.onload = function () {
                    const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
                    const canvas = document.createElement("canvas");
                    canvas.width = Math.round(img.naturalWidth * scale);
                    canvas.height = Math.round(img.naturalHeight * scale);
                    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
                    URL.revokeObjectURL(url);
                    canvas.toBlob(function (blob) { resolve(blob || original); }, "image/jpeg", 0.85);
                };
                img.onerror = function () { URL.revokeObjectURL(url); resolve(original); };   // HEIC on a computer: sent as it is
                img.src = url;
            });
        }

        function say(box, text) {
            box.textContent = text;
            box.hidden = !text;
        }

        function choose(input) {
            const f = input.files && input.files[0];
            input.value = "";
            if (!f) return;
            const pdf = f.type === "application/pdf" || /\.pdf$/i.test(f.name);
            if (!pdf && f.type.indexOf("image/") !== 0 && !/\.(heic|heif)$/i.test(f.name)) {
                pick.querySelector(".hint").textContent = "That is not a photo or a PDF. Take a photo, or choose a photo or PDF of the receipt.";
                return;
            }
            say(error, "");
            say(status, pdf ? "" : "Getting the photo ready…");
            pick.hidden = true;
            chosen.hidden = false;
            ways.hidden = true;
            preview.replaceChildren();
            (pdf ? Promise.resolve(f) : shrink(f)).then(function (ready) {
                if (ready.size > Number(scan.dataset.max)) {
                    say(status, "");
                    say(error, "That file is too big. Take the photo again, or choose a smaller PDF.");
                    ways.hidden = false;
                    ways.querySelectorAll("[data-read]").forEach(function (b) { b.hidden = true; });
                    return;
                }
                file = ready;
                if (pdf) {
                    const p = document.createElement("p");
                    p.className = "receipt-pdf";
                    p.textContent = "PDF: " + f.name;
                    preview.appendChild(p);
                } else {
                    const img = document.createElement("img");
                    img.alt = "The receipt";
                    img.src = URL.createObjectURL(ready);
                    preview.appendChild(img);
                }
                say(status, "");
                ways.hidden = false;
                ways.querySelectorAll("[data-read]").forEach(function (b) { b.hidden = false; b.disabled = false; });
            });
        }

        document.getElementById("receipt-camera").addEventListener("change", function () { choose(this); });
        document.getElementById("receipt-file").addEventListener("change", function () { choose(this); });
        document.getElementById("receipt-again").addEventListener("click", function () {
            file = null;
            chosen.hidden = true;
            pick.hidden = false;
        });

        ways.addEventListener("click", function (e) {
            const button = e.target.closest("[data-read]");
            if (!button || !file) return;
            const gemini = button.dataset.read === "gemini";
            ways.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
            say(error, "");
            say(status, gemini ? "Gemini is reading the receipt… this can take up to a minute." : "Saving the photo…");
            const form = new FormData();
            form.append("csrf", scan.dataset.csrf);
            form.append("read", gemini ? "gemini" : "hand");
            form.append("receipt", file, file.type === "application/pdf" ? "receipt.pdf" : "receipt.jpg");
            fetch(scan.dataset.url, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json" } })
                .then(function (r) {
                    return r.json().catch(function () { return { ok: false, error: "The app did not answer properly. Please try again." }; });
                })
                .then(function (answer) {
                    if (!answer.ok) throw new Error(answer.error);
                    window.location.assign(answer.url);
                })
                .catch(function (err) {
                    say(status, "");
                    say(error, err && err.message && err.message !== "Failed to fetch" ? err.message : "No connection. Try again when you are back online.");
                    ways.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
                });
        });
        return;
    }

    // ---------- The review page ----------
    const form = document.getElementById("receipt-form");
    if (!form) return;
    const rows = document.getElementById("receipt-lines");
    const template = document.getElementById("receipt-line-template");
    const totalBox = document.getElementById("receipt-total");
    const currency = form.dataset.currency || "";
    const printed = parseFloat(form.dataset.printed);
    const SEED_WORD = /\bseeds?\b/i;   // the server's rule for a seed category
    let items = {};
    try { items = JSON.parse(form.dataset.items || "{}"); } catch (e) { /* the server checks everything */ }

    function money(n) {
        return currency + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function show(box, on) {
        if (box) box.hidden = !on;
    }

    function paintLine(row) {
        const choice = row.querySelector("[data-line-item]").value;
        const item = items[choice];
        const isNew = choice === "new";
        const stock = isNew || !!item;
        const seed = item ? item.seed : isNew && SEED_WORD.test(row.querySelector("[data-line-category]").value);
        show(row.querySelector("[data-show-new]"), isNew);
        show(row.querySelector("[data-show-stock]"), stock);
        show(row.querySelector("[data-show-seed]"), isNew && seed);
        show(row.querySelector("[data-hide-seed]"), !seed);
        // Seeds are planted, never sold as they are
        const purpose = row.querySelector("[data-line-purpose]");
        purpose.querySelectorAll("option").forEach(function (o) { if (o.value !== "farm" && seed) o.disabled = true; });
        if (seed) purpose.value = "farm";
        show(row.querySelector("[data-show-both]"), purpose.value === "both");
        const selling = purpose.value !== "farm";
        row.querySelector("[data-price-hint]").textContent = selling ? (item ? "(blank keeps the current price)" : "(needed: it goes on the POS)") : "(optional)";
        const qty = parseInt(row.querySelector("[data-line-qty]").value, 10);
        const cost = parseFloat(row.querySelector("[data-line-cost]").value);
        row.querySelector("[data-line-total]").textContent = qty > 0 && cost >= 0 ? qty.toLocaleString() + " × " + money(cost) + " = " + money(qty * cost) : "";
    }

    function paintTotal() {
        let total = 0;
        rows.querySelectorAll("[data-line]").forEach(function (row) {
            const qty = parseInt(row.querySelector("[data-line-qty]").value, 10);
            const cost = parseFloat(row.querySelector("[data-line-cost]").value);
            if (qty > 0 && cost >= 0) total += qty * cost;
        });
        total = Math.round(total * 100) / 100;
        let words = "Lines add up to " + money(total) + ".";
        totalBox.classList.remove("warn");
        if (isFinite(printed)) {
            if (Math.abs(printed - total) < 0.01) words += " This matches the receipt's total.";
            else { words += " The receipt's total reads " + money(printed) + ": check the lines."; totalBox.classList.add("warn"); }
        }
        totalBox.textContent = words;
    }

    function renumber() {
        const all = rows.querySelectorAll("[data-line]");
        all.forEach(function (row, i) {
            row.querySelector("[data-row-number]").textContent = i + 1;
            row.querySelector("[data-remove-row]").hidden = all.length === 1;
        });
    }

    function paintAll() {
        rows.querySelectorAll("[data-line]").forEach(paintLine);
        renumber();
        paintTotal();
    }

    document.getElementById("receipt-add").addEventListener("click", function () {
        const row = template.content.firstElementChild.cloneNode(true);
        rows.appendChild(row);
        paintAll();
        row.querySelector("[data-line-name]").focus();
    });
    rows.addEventListener("click", function (e) {
        const remove = e.target.closest("[data-remove-row]");
        if (!remove) return;
        remove.closest("[data-line]").remove();
        paintAll();
    });
    rows.addEventListener("input", function (e) {
        const row = e.target.closest("[data-line]");
        if (!row) return;
        paintLine(row);
        paintTotal();
    });
    rows.addEventListener("change", function (e) {
        const row = e.target.closest("[data-line]");
        if (!row) return;
        // Choosing an inventory item starts from what it is usually bought for, and names the line after it if it has no name yet
        if (e.target.matches("[data-line-item]")) {
            const item = items[e.target.value];
            if (item) {
                const purpose = row.querySelector("[data-line-purpose]");
                if (item.purpose && purpose.querySelector('option[value="' + item.purpose + '"]:not([disabled])')) purpose.value = item.purpose;
                const name = row.querySelector("[data-line-name]");
                if (!name.value.trim()) name.value = item.name;
            }
        }
        paintLine(row);
        paintTotal();
    });

    // Payment: a part payment asks how much; paid at all asks how; a named supplier needs the reference
    const pay = document.getElementById("receipt-status-pay");
    const supplier = document.getElementById("receipt-supplier");
    const phone = document.getElementById("receipt-phone");
    const reference = document.getElementById("receipt-reference");
    function paintPay() {
        const paying = pay.value !== "Unpaid";
        show(document.getElementById("receipt-paid-row"), pay.value === "Partial Payment");
        show(document.getElementById("receipt-method-row"), paying);
        show(document.getElementById("receipt-reference-row"), paying);
        document.getElementById("receipt-method").required = paying;
        document.getElementById("receipt-method").disabled = !paying;
        reference.disabled = !paying;
        const named = supplier.value.trim() !== "";
        reference.required = paying && named;
        show(document.getElementById("receipt-reference-optional"), !named);
        if (paying && named && !reference.value.trim()) reference.placeholder = "for cash, the receipt number" + (document.getElementById("receipt-no").value.trim() ? " (" + document.getElementById("receipt-no").value.trim() + ")" : "");
    }
    pay.addEventListener("change", paintPay);
    supplier.addEventListener("input", paintPay);
    // A known supplier fills in their phone
    supplier.addEventListener("change", function () {
        const match = Array.from(document.querySelectorAll("#receipt-suppliers option")).find(function (o) { return o.value.toLowerCase() === supplier.value.trim().toLowerCase(); });
        if (match && !phone.value.trim()) phone.value = match.dataset.phone || "";
    });
    paintPay();
    paintAll();
})();
