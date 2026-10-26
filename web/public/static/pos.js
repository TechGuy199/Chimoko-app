(function () {
    const form = document.getElementById("checkout");
    const rows = Array.from(document.querySelectorAll("#products tbody tr[data-id]"));
    const cartBody = document.querySelector("#cart tbody");
    const cartEmpty = document.getElementById("cart-empty");
    const totalEl = document.getElementById("cart-total");
    const search = document.getElementById("search");
    const deptFilter = document.getElementById("dept-filter");
    const status = document.getElementById("payment-status");
    const amountRow = document.getElementById("amount-row");
    const amountPaid = document.getElementById("amount-paid");
    const currency = form.dataset.currency;
    const cart = new Map();

    function money(n) {
        return currency + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function cell(text, className) {
        const td = document.createElement("td");
        td.textContent = text;
        if (className) td.className = className;
        return td;
    }

    function add(row) {
        const id = row.dataset.id;
        const stock = parseInt(row.dataset.stock, 10);
        const item = cart.get(id) || { id: id, name: row.dataset.name, price: parseFloat(row.dataset.price), stock: stock, qty: 0 };
        if (item.qty >= stock) {
            alert("Only " + stock + " in stock for " + item.name + ".");
            return;
        }
        item.qty += 1;
        cart.set(id, item);
        render();
        // Something to see where the tap was, since the cart itself may be far down the page (on a phone)
        row.classList.remove("added");
        void row.offsetWidth;
        row.classList.add("added");
        cartBar.classList.remove("bump");
        void cartBar.offsetWidth;
        cartBar.classList.add("bump");
    }

    // The cart bar: while the cart has something in it and is off screen, how many items and the total, and a tap to go there
    const cartBar = document.getElementById("cart-bar");
    const cartSection = document.getElementById("cart-section");
    // Out of the page's main area, whose entrance animation would otherwise pin it to the page instead of the screen (a page
    // change replaces the whole body, so it never outlives the POS)
    document.body.appendChild(cartBar);
    // Straight to the cart (a long list is no place for a slow scroll), and a glow to show where it landed
    cartBar.addEventListener("click", function (e) {
        e.preventDefault();
        cartSection.scrollIntoView({ behavior: "instant", block: "start" });
        cartSection.classList.remove("arrived");
        void cartSection.offsetWidth;
        cartSection.classList.add("arrived");
    });
    let cartInView = false;
    function updateCartBar(total) {
        let items = 0;
        cart.forEach(function (item) { items += item.qty; });
        document.getElementById("cart-bar-count").textContent = items + (items === 1 ? " item" : " items");
        document.getElementById("cart-bar-total").textContent = money(total);
        cartBar.hidden = cart.size === 0 || cartInView;
    }
    if ("IntersectionObserver" in window) {
        new IntersectionObserver(function (entries) {
            cartInView = entries[0].isIntersecting;
            cartBar.hidden = cart.size === 0 || cartInView;
        }, { rootMargin: "0px 0px -80px 0px" }).observe(cartSection);
    }

    function render() {
        cartBody.replaceChildren();
        let total = 0;
        cart.forEach(function (item) {
            const tr = document.createElement("tr");
            const nameCell = cell(item.name);
            if (item.preorder) {
                // A pre-order goes on the invoice as it was agreed: its quantity and price cannot be changed here
                const tag = document.createElement("small");
                tag.className = "muted";
                tag.textContent = " pre-order, " + money(item.price) + " each";
                nameCell.appendChild(tag);
            }
            tr.appendChild(nameCell);

            const qtyCell = document.createElement("td");
            if (item.preorder) {
                qtyCell.textContent = item.qty;
                qtyCell.className = "num";
            } else {
                const qty = document.createElement("input");
                qty.type = "number";
                qty.min = 1;
                qty.max = item.stock;
                qty.value = item.qty;
                qty.className = "qty";
                qty.addEventListener("change", function () {
                    const wanted = parseInt(qty.value, 10);
                    item.qty = Math.min(item.stock, Math.max(1, isNaN(wanted) ? 1 : wanted));
                    render();
                });
                qtyCell.appendChild(qty);
            }
            tr.appendChild(qtyCell);

            tr.appendChild(cell(money(item.qty * item.price), "num"));

            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn danger small";
            remove.textContent = "×";
            remove.title = "Remove";
            remove.addEventListener("click", function () {
                cart.delete(item.id);
                render();
            });
            const removeCell = document.createElement("td");
            removeCell.className = "num";
            removeCell.appendChild(remove);
            tr.appendChild(removeCell);

            cartBody.appendChild(tr);
            total += item.qty * item.price;
        });
        cartEmpty.hidden = cart.size > 0;
        totalEl.textContent = money(total);
        updateCartBar(total);
    }

    function applyFilter() {
        const q = search.value.trim().toLowerCase();
        const dept = deptFilter.value;
        rows.forEach(function (row) {
            const matchesText = !q || row.dataset.name.toLowerCase().includes(q) || row.dataset.code.toLowerCase().includes(q);
            const matchesDept = !dept || row.dataset.dept === dept;
            row.hidden = !(matchesText && matchesDept);
        });
    }

    rows.forEach(function (row) {
        row.querySelector("button").addEventListener("click", function () { add(row); });
    });

    search.addEventListener("input", applyFilter);
    deptFilter.addEventListener("change", applyFilter);

    // USB barcode scanners type the code and press Enter: an exact code match goes straight into the cart
    search.addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const code = search.value.trim().toLowerCase();
        const hit = rows.find(function (row) { return row.dataset.code.toLowerCase() === code; });
        if (hit) {
            add(hit);
            search.value = "";
            applyFilter();
        }
    });

    // Picking a known customer fills in their phone number, so repeat sales land on the same account
    const customerName = document.getElementById("customer-name");
    const customerPhone = document.getElementById("customer-phone");
    customerName.addEventListener("change", function () {
        const typed = customerName.value.trim().toLowerCase();
        const matches = Array.from(document.querySelectorAll("#known-customers option")).filter(function (option) {
            return option.value.toLowerCase() === typed;
        });
        if (matches.length === 1 && !customerPhone.value.trim()) customerPhone.value = matches[0].dataset.phone;
    });

    // How it was paid is asked whenever money is being taken; for an unpaid sale it is hidden and switched off, so it is not sent
    const methodRow = document.getElementById("pay-method-row");
    const method = document.getElementById("pay-method");
    const reference = document.getElementById("pay-reference");
    function updatePayment() {
        const partial = status.value === "Partial Payment";
        const paying = status.value !== "Unpaid";
        amountRow.hidden = !partial;
        amountPaid.required = partial;
        methodRow.hidden = !paying;
        method.disabled = !paying;
        reference.disabled = !paying;
    }
    status.addEventListener("change", updatePayment);
    updatePayment();

    // Taken now, or collected on another day: the day is asked for (and sent) only for another day
    const collectRow = document.getElementById("collect-day-row");
    const collectHint = document.getElementById("collect-day-hint");
    const collectOn = document.getElementById("collect-on");
    const collection = Array.from(form.querySelectorAll('input[name="collection"]'));
    function updateCollection() {
        const later = collection.some(function (radio) { return radio.checked && radio.value === "later"; });
        collectRow.hidden = !later;
        collectHint.hidden = !later;
        collectOn.disabled = !later;
        collectOn.required = later;
    }
    collection.forEach(function (radio) { radio.addEventListener("change", updateCollection); });

    // Pre-orders sent from Up Next ("Generate invoice"): into the cart, with the customer's details, and their collection day if it
    // is still to come
    const preorders = document.getElementById("preorder-lines");
    let sent = [];
    try {
        sent = JSON.parse(preorders.dataset.lines || "[]");
    } catch (err) {
        sent = [];
    }
    if (sent.length) {
        sent.forEach(function (po) {
            cart.set("po-" + po.preorder, { id: "po-" + po.preorder, preorder: po.preorder, name: po.name, price: po.price, stock: po.qty, qty: po.qty });
        });
        if (!customerName.value.trim()) customerName.value = sent[0].customer_name;
        if (!customerPhone.value.trim()) customerPhone.value = sent[0].customer_phone;
        const coming = sent.map(function (po) { return po.collect_on; }).filter(function (day) { return day && day >= preorders.dataset.tomorrow; }).sort();
        if (coming.length) {
            collection.forEach(function (radio) { radio.checked = radio.value === "later"; });
            collectOn.value = coming[0];
        }
        render();
    }
    updateCollection();

    form.addEventListener("submit", function (e) {
        if (cart.size === 0) {
            e.preventDefault();
            alert("Add at least one item to the cart first.");
            return;
        }
        const lines = [];
        cart.forEach(function (item) { lines.push(item.preorder ? { preorder: item.preorder } : { id: item.id, qty: item.qty }); });
        document.getElementById("cart-data").value = JSON.stringify(lines);
    });
})();
