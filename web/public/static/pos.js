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
    }

    function render() {
        cartBody.replaceChildren();
        let total = 0;
        cart.forEach(function (item) {
            const tr = document.createElement("tr");
            tr.appendChild(cell(item.name));

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
            const qtyCell = document.createElement("td");
            qtyCell.appendChild(qty);
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

    status.addEventListener("change", function () {
        const partial = status.value === "Partial Payment";
        amountRow.hidden = !partial;
        amountPaid.required = partial;
    });

    form.addEventListener("submit", function (e) {
        if (cart.size === 0) {
            e.preventDefault();
            alert("Add at least one item to the cart first.");
            return;
        }
        const lines = [];
        cart.forEach(function (item) { lines.push({ id: item.id, qty: item.qty }); });
        document.getElementById("cart-data").value = JSON.stringify(lines);
    });
})();
