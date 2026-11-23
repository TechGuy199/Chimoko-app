// The offline screens (offline.html): this device's snapshot of the business, and a till that works with no connection. A sale made
// here is kept on the device with a temporary number (OFF-...) and uploaded, through the same checks as any sale, when the connection is
// back; the receipt says so. Only text is ever put on the page from the snapshot (textContent), never markup.
(function () {
    "use strict";
    const O = window.MSOffline;
    const view = document.getElementById("off-view");
    const tabs = Array.from(document.querySelectorAll("[data-tab]"));
    let snap = null, all = [], tab = "sell", cart = [], receipt = null, planted = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function money(amount) {
        return (snap ? snap.business.currency : "") + Number(amount || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function when(stamp) {
        const d = new Date(String(stamp).replace(" ", "T"));
        if (isNaN(d)) return stamp || "";
        return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + ", " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    }

    function card(title) {
        const c = el("section", "card block");
        if (title) c.appendChild(el("h2", "", title));
        view.appendChild(c);
        return c;
    }

    function table(headings, rows, numeric) {
        const wrap = el("div", "table-wrap");
        const t = el("table");
        const head = el("tr");
        headings.forEach(function (h, i) { head.appendChild(el("th", numeric && numeric[i] ? "num" : "", h)); });
        t.appendChild(el("thead")).appendChild(head);
        const body = el("tbody");
        rows.forEach(function (cells) {
            const tr = el("tr");
            cells.forEach(function (c, i) {
                const td = el("td", numeric && numeric[i] ? "num" : "");
                if (c instanceof Node) td.appendChild(c); else td.textContent = c === null || c === undefined ? "" : c;
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
        t.appendChild(body);
        wrap.appendChild(t);
        return wrap;
    }

    // A name with its code underneath, small: one column instead of two, so a table fits a phone
    function named(name, code, extra) {
        const cell = el("span", "offline-named");
        cell.appendChild(el("span", "", name + (extra || "")));
        cell.appendChild(el("small", "muted", code));
        return cell;
    }

    function search(placeholder, onInput) {
        const box = el("input");
        box.type = "search";
        box.placeholder = placeholder;
        box.setAttribute("aria-label", placeholder);
        box.addEventListener("input", function () { onInput(box.value.trim().toLowerCase()); });
        return box;
    }

    function has(q) {
        const words = Array.prototype.slice.call(arguments, 1).join(" ").toLowerCase();
        return !q || words.indexOf(q) >= 0;
    }

    // ---------- Where the copy stands ----------
    function paintHead() {
        const asof = document.getElementById("off-asof");
        const online = O.reachable ? O.reachable() : navigator.onLine;
        document.getElementById("off-online").hidden = !online;
        if (!snap) {
            asof.className = "flash error";
            asof.textContent = "This device has no offline copy yet. Open the app once with a connection while signed in, and it keeps one from then on.";
            return;
        }
        document.getElementById("off-business").textContent = snap.business.name;
        const hours = (Date.now() - Date.parse(snap.at.replace(" ", "T"))) / 3600000;
        asof.className = "flash no-print " + (hours > 24 ? "error" : "notice");
        const checking = O.checking && O.checking();
        asof.textContent = (online ? "Connected. " : checking ? "Checking the connection. " : "No connection. ") + "Prices, stock and records as they were at " + when(snap.at) +
            (hours > 24 ? ": more than a day ago, so check before relying on them." : ".") +
            (snap.user ? " Signed in as " + snap.user.name + "." : "");
        const waiting = all.filter(function (s) { return s.status !== "uploaded"; }).length;
        const badge = document.getElementById("off-count");
        badge.hidden = !waiting;
        badge.textContent = waiting;
    }

    // ---------- Sell ----------
    function discountOf(text, gross) {
        const t = String(text || "").trim().replace(/,/g, "");
        if (!t) return 0;
        const pct = t.match(/^(\d+(?:\.\d+)?)\s*%$/);
        const amount = pct ? gross * Number(pct[1]) / 100 : Number(t);
        if (isNaN(amount) || amount < 0 || amount > gross + 0.001 || (pct && Number(pct[1]) > 100)) return null;
        return Math.round(amount * 100) / 100;
    }

    function cartLines() {
        return cart.map(function (line) {
            const p = snap.products.find(function (x) { return x.id === line.id; });
            const gross = Math.round(line.qty * p.price * 100) / 100;
            const discount = discountOf(line.discount, gross);
            return { p: p, line: line, gross: gross, discount: discount, total: discount === null ? null : Math.round((gross - discount) * 100) / 100 };
        });
    }

    function showSell() {
        if (receipt) { showReceipt(); return; }
        if (!snap.user.sell) { card("Sell").appendChild(el("p", "muted", "Your account cannot record sales. Ask an admin.")); return; }
        const pick = card("Sell");
        const list = el("div", "offline-products");
        function paint(q) {
            list.replaceChildren();
            const found = snap.products.filter(function (p) { return has(q, p.name, p.item_code, p.department); }).slice(0, 40);
            if (!found.length) list.appendChild(el("p", "muted", "Nothing listed on the POS fits that."));
            found.forEach(function (p) {
                const row = el("div", "offline-product");
                const name = el("div");
                name.appendChild(el("strong", "", p.name));
                name.appendChild(el("small", "muted", p.item_code + " · " + money(p.price) + " · " + p.quantity + " in stock (as of the copy)"));
                row.appendChild(name);
                const add = el("button", "btn small primary", "Add");
                add.type = "button";
                add.addEventListener("click", function () {
                    const there = cart.find(function (c) { return c.id === p.id; });
                    if (there) there.qty += 1; else cart.push({ id: p.id, qty: 1, discount: "" });
                    paintCart();
                });
                row.appendChild(add);
                list.appendChild(row);
            });
        }
        pick.appendChild(search("Search name or code", paint));
        pick.appendChild(list);
        paint("");

        const box = card("This sale");
        const lines = el("div");
        box.appendChild(lines);
        const form = el("div", "offline-sale-form");
        function field(label, input) {
            const l = el("label", "", label + " ");
            l.appendChild(input);
            form.appendChild(l);
            return input;
        }
        const customer = field("Customer name", el("input"));
        customer.setAttribute("list", "off-customers");
        const names = el("datalist");
        names.id = "off-customers";
        snap.customers.slice(0, 500).forEach(function (c) { const o = el("option"); o.value = c.name; names.appendChild(o); });
        form.appendChild(names);
        const phone = field("Phone", el("input"));
        phone.type = "tel";
        customer.addEventListener("change", function () {
            const c = snap.customers.find(function (x) { return x.name === customer.value; });
            if (c && !phone.value) phone.value = c.phone || "";
        });
        const status = field("Payment", el("select"));
        snap.statuses.forEach(function (s) { const o = el("option", "", s); o.value = s; status.appendChild(o); });
        const paid = field("Amount paid now", el("input"));
        paid.type = "number";
        paid.step = "0.01";
        paid.min = "0";
        const method = field("Paid by", el("select"));
        snap.methods.forEach(function (m) { const o = el("option", "", m); o.value = m; method.appendChild(o); });
        const reference = field("Reference (optional)", el("input"));
        function paintPayment() {
            paid.parentNode.hidden = status.value !== "Partial Payment";
            method.parentNode.hidden = reference.parentNode.hidden = status.value === "Unpaid";
        }
        status.addEventListener("change", paintPayment);
        paintPayment();
        box.appendChild(form);
        const problem = el("p", "flash error");
        problem.hidden = true;
        box.appendChild(problem);
        const done = el("button", "btn primary", "Record sale");
        done.type = "button";
        box.appendChild(done);
        box.appendChild(el("p", "muted", "Recorded on this device with a temporary number, and uploaded as a sale (with its invoice number) when the connection is back."));

        function paintCart() {
            lines.replaceChildren();
            if (!cart.length) { lines.appendChild(el("p", "muted", "Add products from the list.")); return; }
            const rows = cartLines().map(function (l) {
                const qty = el("input");
                qty.type = "number";
                qty.min = "1";
                qty.step = "1";
                qty.value = l.line.qty;
                qty.className = "offline-qty";
                qty.setAttribute("aria-label", "Quantity of " + l.p.name);
                qty.addEventListener("change", function () { l.line.qty = Math.max(1, Math.floor(Number(qty.value) || 1)); paintCart(); });
                const off = el("input");
                off.value = l.line.discount;
                off.placeholder = "e.g. 5 or 10%";
                off.className = "offline-discount";
                off.setAttribute("aria-label", "Discount on " + l.p.name);
                off.addEventListener("change", function () { l.line.discount = off.value; paintCart(); });
                const remove = el("button", "btn small ghost", "Remove");
                remove.type = "button";
                remove.addEventListener("click", function () { cart = cart.filter(function (c) { return c !== l.line; }); paintCart(); });
                const short = l.line.qty > l.p.quantity ? " (the copy shows " + l.p.quantity + ": check the shelf)" : "";
                return [l.p.name + short, qty, money(l.p.price), off, l.total === null ? "Check the discount" : money(l.total), remove];
            });
            lines.appendChild(table(["Product", "Qty", "Price", "Discount", "Total", ""], rows, [0, 1, 1, 0, 1, 0]));
            const total = cartLines().reduce(function (sum, l) { return sum + (l.total || 0); }, 0);
            lines.appendChild(el("p", "offline-total", "Total " + money(total)));
        }
        paintCart();

        done.addEventListener("click", function () {
            problem.hidden = true;
            const ls = cartLines();
            const total = Math.round(ls.reduce(function (sum, l) { return sum + (l.total || 0); }, 0) * 100) / 100;
            let error = !ls.length ? "Add at least one product." : ls.some(function (l) { return l.total === null; }) ? "A discount is not right: an amount up to the line's price, or a percentage like 10%." : "";
            const amount = status.value === "Paid" ? total : status.value === "Unpaid" ? 0 : Math.round(Number(paid.value) * 100) / 100;
            if (!error && status.value === "Partial Payment" && !(amount > 0 && amount < total)) error = "A partial payment must be more than zero and less than the total.";
            if (error) { problem.textContent = error; problem.hidden = false; return; }
            const fields = { customer_name: customer.value.trim(), customer_phone: phone.value.trim(), payment_status: status.value, handover: "now" };
            if (status.value === "Partial Payment") fields.amount_paid = String(amount);
            if (amount > 0) { fields.payment_method = method.value; fields.payment_reference = reference.value.trim(); }
            const now = new Date();
            const pad = function (n) { return String(n).padStart(2, "0"); };
            const sale = {
                user: snap.user.id, userName: snap.user.name, fields: fields, total: total, paid: amount,
                made_at: now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) + " " + pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds()),
                cart: ls.map(function (l) { return { id: l.p.id, qty: l.line.qty, discount: l.line.discount }; }),
                prices: ls.reduce(function (m, l) { m[l.p.id] = l.p.price; return m; }, {}),
                lines: ls.map(function (l) { return [l.p.name, l.line.qty, l.p.price, l.total]; }),
            };
            done.disabled = true;
            O.record(sale).then(function (saved) {
                receipt = saved;
                cart = [];
                load();
            }, function () {
                done.disabled = false;
                problem.textContent = "This device could not keep the sale (its storage may be full or blocked). Write it down and enter it later.";
                problem.hidden = false;
            });
        });
    }

    function showReceipt() {
        const s = receipt;
        const c = card("");
        c.classList.add("offline-receipt");
        c.appendChild(el("h2", "", snap.business.name));
        c.appendChild(el("p", "", "Offline sale " + s.ref + " · " + when(s.made_at)));
        c.appendChild(el("p", "", "Customer: " + (s.fields.customer_name || snap.business.walk_in) + (s.fields.customer_phone ? " · " + s.fields.customer_phone : "")));
        c.appendChild(table(["Product", "Qty", "Price", "Total"], s.lines.map(function (l) { return [l[0], l[1], money(l[2]), money(l[3])]; }), [0, 1, 1, 1]));
        c.appendChild(el("p", "offline-total", "Total " + money(s.total) + " · Paid " + money(s.paid) + (s.total - s.paid > 0.005 ? " · Owing " + money(s.total - s.paid) : "")));
        c.appendChild(el("p", "muted", "Made while offline. Its invoice number is given when it is uploaded; " + s.ref + " stays on it so the two can be matched."));
        const row = el("div", "toolbar no-print");
        const print = el("button", "btn", "Print receipt");
        print.type = "button";
        print.addEventListener("click", function () { window.print(); });
        const next = el("button", "btn primary", "New sale");
        next.type = "button";
        next.addEventListener("click", function () { receipt = null; show(); });
        row.appendChild(print);
        row.appendChild(next);
        c.appendChild(row);
    }

    // ---------- Viewing ----------
    function showStock() {
        const c = card("Stock (as of the copy)");
        const out = el("div");
        function paint(q) {
            out.replaceChildren();
            const low = snap.business.low;
            out.appendChild(el("h3", "", "On the POS"));
            out.appendChild(table(["Product", "In stock", "Price"], snap.products.filter(function (p) { return has(q, p.name, p.item_code, p.department); }).slice(0, 300)
                .map(function (p) { return [named(p.name, p.item_code, p.quantity < low ? " (low)" : ""), p.quantity, money(p.price)]; }), [0, 1, 1]));
            out.appendChild(el("h3", "", "Inventory"));
            out.appendChild(table(["Item", "Quantity", "Cost", "Price"], snap.inventory.filter(function (p) { return has(q, p.name, p.item_code, p.category); }).slice(0, 300)
                .map(function (p) { return [named(p.name, p.item_code + (p.category ? " \u00b7 " + p.category : "")), p.quantity + (p.unit ? " " + p.unit : ""), money(p.unit_cost), money(p.price)]; }), [0, 1, 1, 1]));
        }
        c.appendChild(search("Search name, code or category", paint));
        c.appendChild(out);
        paint("");
    }

    function showCustomers() {
        const c = card("Customers");
        const out = el("div");
        const admin = snap.user.admin;
        function paint(q) {
            out.replaceChildren();
            out.appendChild(table(admin ? ["Customer", "Phone", "Owes"] : ["Customer", "Phone"],
                snap.customers.filter(function (x) { return has(q, x.name, x.phone); }).slice(0, 300)
                    .map(function (x) { return admin ? [x.name, x.phone, x.owes > 0.005 ? money(x.owes) : "—"] : [x.name, x.phone]; }), [0, 0, 1]));
        }
        c.appendChild(search("Search name or phone", paint));
        c.appendChild(out);
        paint("");
    }

    function showInvoices() {
        const c = card("Invoices (the last " + snap.days + " days)");
        const out = el("div");
        function paint(q) {
            out.replaceChildren();
            const pending = all.filter(function (s) { return s.kind !== "planting" && s.status !== "uploaded" && has(q, s.ref, s.fields.customer_name); });
            if (pending.length) {
                out.appendChild(el("h3", "", "Made offline, not uploaded yet"));
                out.appendChild(table(["Number", "When", "Customer", "Total"], pending.map(function (s) { return [s.ref, when(s.made_at), s.fields.customer_name || snap.business.walk_in, money(s.total)]; }), [0, 0, 0, 1]));
            }
            const found = snap.invoices.filter(function (i) { return has(q, i.invoice_no, i.customer_name, i.customer_phone, i.offline_ref); }).slice(0, 200);
            const list = el("div", "offline-invoices");
            found.forEach(function (i) {
                const d = el("details", "offline-invoice");
                const s = el("summary");
                s.appendChild(el("strong", "", i.invoice_no));
                s.appendChild(el("span", "", " " + when(i.created_at) + " · " + (i.customer_name || "") + " · " + money(i.total) + " · " + i.payment_status +
                    (i.balance_due > 0.005 ? " · owes " + money(i.balance_due) : "")));
                d.appendChild(s);
                d.appendChild(table(["Item", "Qty", "Price", "Total"], i.items.map(function (l) { return [l[0], l[1], money(l[2]), money(l[3])]; }), [0, 1, 1, 1]));
                list.appendChild(d);
            });
            if (!found.length) list.appendChild(el("p", "muted", "No invoice in the copy fits that."));
            out.appendChild(list);
        }
        c.appendChild(search("Search number, customer or phone", paint));
        c.appendChild(out);
        paint("");
    }

    function today() {
        const d = new Date();
        const pad = function (n) { return String(n).padStart(2, "0"); };
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    }

    function plusDays(day, days) {
        const parts = day.split("-").map(Number);
        const d = new Date(parts[0], parts[1] - 1, parts[2] + days);
        const pad = function (n) { return String(n).padStart(2, "0"); };
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    }

    function labelled(parent, text, input) {
        const l = el("label", "", text + " ");
        l.appendChild(input);
        parent.appendChild(l);
        return input;
    }

    // Trays planted, entered with no connection: who planted, on which day, and an entry for each seed item. Kept on this device and
    // uploaded when the connection is back, where the app checks it like any other entry and takes the seeds out of stock.
    function showPlantForm() {
        const P = snap.planting;
        if (planted) {
            const done = card("Kept on this device");
            done.appendChild(el("p", "", planted.ref + ": " + planted.summary));
            done.appendChild(el("p", "muted", "It uploads by itself when the connection is back, and the seeds come out of stock then. Until then it waits under Waiting."));
            const more = el("button", "btn primary", "Log more trays");
            more.type = "button";
            more.addEventListener("click", function () { planted = null; show(); });
            done.appendChild(more);
            return;
        }
        const c = card("Log trays planted");
        if (!P || !P.planters.length) {
            c.appendChild(el("p", "muted", P ? "No one is set to plant trays. An admin ticks \"Plants trays\" for them in HR." : "Open the app once with a connection to bring the planting form onto this device."));
            return;
        }
        const usable = P.seeds.filter(function (x) { return x.usable; });
        if (!usable.length) { c.appendChild(el("p", "muted", "No seed item in the copy has stock you can plant from.")); return; }
        const form = el("div", "offline-plant-form");
        const who = labelled(form, "Who planted", el("select"));
        const none = el("option", "", "Choose the person");
        none.value = "";
        who.appendChild(none);
        P.planters.forEach(function (e) { const o = el("option", "", e.name); o.value = e.id; who.appendChild(o); });
        const day = labelled(form, "Day planted", el("input"));
        day.type = "date";
        day.value = day.max = today();
        c.appendChild(form);

        const rows = el("div");
        c.appendChild(rows);
        function addRow() {
            const box = el("fieldset", "activity-row offline-plant-row");
            box.appendChild(el("legend", "", "Entry"));
            const item = labelled(box, "Seed", el("select"));
            const pick = el("option", "", "Choose the seed item");
            pick.value = "";
            item.appendChild(pick);
            usable.forEach(function (x) {
                const o = el("option", "", x.name + " (" + x.code + ") \u2014 " + x.packs + " in stock" + (x.loose ? " + " + x.loose.toLocaleString() + " loose" : ""));
                o.value = x.id;
                item.appendChild(o);
            });
            const grid = el("div", "grid");
            box.appendChild(grid);
            const trays = labelled(grid, "Trays planted", el("input"));
            trays.type = "number"; trays.min = "0.01"; trays.step = "any"; trays.inputMode = "decimal";
            let kind = null;
            if (P.kinds.length) {
                kind = labelled(grid, "Kind of tray", el("select"));
                const k0 = el("option", "", "Choose the kind");
                k0.value = "";
                kind.appendChild(k0);
                P.kinds.forEach(function (k) { const o = el("option", "", k.name); o.value = k.id; kind.appendChild(o); });
            }
            const holes = labelled(grid, "Holes in each tray", el("input"));
            holes.type = "number"; holes.min = "1"; holes.max = P.max_holes; holes.step = "1"; holes.inputMode = "numeric";
            if (P.usual_holes) holes.value = P.usual_holes;
            const mortality = labelled(grid, "Expected mortality (%)", el("input"));
            mortality.type = "number"; mortality.min = "0"; mortality.max = "99.99"; mortality.step = "any"; mortality.inputMode = "decimal";
            const ready = labelled(grid, "Expected ready on", el("input"));
            ready.type = "date";
            const note = el("p", "seedling-summary");
            box.appendChild(note);
            const remove = el("button", "btn small ghost", "Remove this entry");
            remove.type = "button";
            remove.addEventListener("click", function () { if (rows.children.length > 1) box.remove(); });
            box.appendChild(remove);
            function paintNote() {
                const t = parseFloat(trays.value), h = parseInt(holes.value, 10);
                note.textContent = t > 0 && h > 0 ? t.toLocaleString() + (t === 1 ? " tray" : " trays") + " \u00d7 " + h.toLocaleString() + " holes = " + Math.floor(t * h + 0.5).toLocaleString() + " seeds." : "";
            }
            item.addEventListener("change", function () {
                const seed = usable.find(function (x) { return String(x.id) === item.value; });
                if (!seed) return;
                if (seed.mortality !== null && !mortality.value) mortality.value = seed.mortality;
                if (seed.grow !== null && !ready.value && day.value) ready.value = plusDays(day.value, seed.grow);
            });
            if (kind) kind.addEventListener("change", function () {
                const k = P.kinds.find(function (x) { return String(x.id) === kind.value; });
                if (k && k.holes) holes.value = k.holes;
                paintNote();
            });
            trays.addEventListener("input", paintNote);
            holes.addEventListener("input", paintNote);
            box.read = function () { return { item: item, trays: trays, kind: kind, holes: holes, mortality: mortality, ready: ready }; };
            rows.appendChild(box);
        }
        addRow();
        const add = el("button", "btn", "+ Add another entry");
        add.type = "button";
        add.addEventListener("click", addRow);
        c.appendChild(add);
        const problem = el("p", "flash error");
        problem.hidden = true;
        c.appendChild(problem);
        const save = el("button", "btn success wide", "Save entries");
        save.type = "button";
        c.appendChild(save);
        c.appendChild(el("p", "muted", "Kept on this device and uploaded when the connection is back. The app then checks the stock and takes the seeds out of inventory; an entry it cannot accept waits under Waiting with the reason."));

        save.addEventListener("click", function () {
            problem.hidden = true;
            const person = P.planters.find(function (e) { return String(e.id) === who.value; });
            let error = !person ? "Choose who planted." : !day.value || day.value > today() ? "Choose the day planted (today or earlier)." : "";
            const lines = [];
            Array.from(rows.children).forEach(function (box, i) {
                if (error) return;
                const f = box.read();
                const seed = usable.find(function (x) { return String(x.id) === f.item.value; });
                const t = parseFloat(f.trays.value), h = parseInt(f.holes.value, 10), m = parseFloat(f.mortality.value);
                const k = f.kind ? P.kinds.find(function (x) { return String(x.id) === f.kind.value; }) : null;
                const n = "Entry " + (i + 1) + ": ";
                if (!seed) error = n + "choose the seed item.";
                else if (!(t > 0)) error = n + "enter the trays planted.";
                else if (f.kind && !k) error = n + "choose the kind of tray.";
                else if (!(h >= 1)) error = n + "enter the holes in each tray.";
                else if (!(m >= 0 && m < 100)) error = n + "enter the expected mortality, from 0 up to (not including) 100.";
                else if (!f.ready.value || f.ready.value < day.value) error = n + "enter the day the seedlings are expected to be ready (not before the day planted).";
                else lines.push({ item_id: seed.id, item: seed.name, trays: t, holes: h, kind_id: k ? k.id : "", kind: k ? k.name : "", mortality: m, ready_on: f.ready.value });
            });
            if (error) { problem.textContent = error; problem.hidden = false; return; }
            const now = new Date();
            const pad = function (n) { return String(n).padStart(2, "0"); };
            const entry = {
                user: snap.user.id, userName: snap.user.name, employee_id: person.id, employee: person.name, date: day.value, entries: lines,
                made_at: now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) + " " + pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds()),
                summary: person.name + ", " + day.value + ": " + lines.map(function (l) { return l.trays.toLocaleString() + (l.kind ? " " + l.kind : "") + (l.trays === 1 ? " tray" : " trays") + " of " + l.item; }).join("; ") + ".",
            };
            save.disabled = true;
            O.recordPlanting(entry).then(function (saved) {
                planted = saved;
                load();
            }, function () {
                save.disabled = false;
                problem.textContent = "This device could not keep the entries (its storage may be full or blocked). Write them down and enter them later.";
                problem.hidden = false;
            });
        });
    }

    function showPlantings() {
        showPlantForm();
        const c = card("Batches in the greenhouse");
        c.appendChild(table(["Batch", "Planted", "Ready", "Stage", "Seedlings", "Free", "Pre-ordered"],
            snap.plantings.map(function (b) { return [b.batch, b.planted_on, b.ready_on, b.stage, b.seedlings, b.free, b.preordered]; }), [0, 0, 0, 0, 1, 1, 1]));
        const p = card("Pre-orders waiting");
        p.appendChild(table(["Customer", "Phone", "Batch", "Qty", "Collect on"],
            snap.preorders.map(function (x) { return [x.customer, x.phone, x.batch, x.qty, x.collect_on || "When ready"]; }), [0, 0, 0, 1, 0]));
    }

    // ---------- Sales waiting to upload ----------
    function showWaiting() {
        const c = card("Made offline: sales and planting entries");
        const row = el("div", "toolbar");
        const reachable = O.reachable ? O.reachable() : navigator.onLine;
        const now = el("button", "btn primary", reachable ? "Upload now" : "Uploads when the connection is back");
        now.type = "button";
        now.disabled = !reachable;
        now.addEventListener("click", function () { now.disabled = true; now.textContent = "Uploading…"; O.upload().then(load); });
        row.appendChild(now);
        c.appendChild(row);
        if (!all.length) { c.appendChild(el("p", "muted", "Nothing made offline on this device.")); return; }
        const sorted = all.slice().sort(function (a, b) { return a.made_at < b.made_at ? 1 : -1; });
        c.appendChild(table(["Number", "When", "By", "What", "Total", "Where it stands", ""], sorted.map(function (s) {
            let where;
            if (s.status === "uploaded") {
                where = el("a", "", s.kind === "planting" ? "Uploaded: open the day" : "Uploaded as " + s.invoice_no);
                where.href = s.url;
            } else if (s.status === "attention") {
                where = el("span", "warn", "Needs attention: " + s.problem);
            } else {
                where = el("span", "", snap && String(s.user) !== String(snap.user.id) ? "Waiting for " + s.userName + " to sign in" : "Waiting to upload");
            }
            const act = el("span");
            if (s.status === "attention") {
                const retry = el("button", "btn small", "Try again");
                retry.type = "button";
                retry.addEventListener("click", function () { s.status = "waiting"; delete s.problem; O.putSale(s).then(function () { return O.upload(); }).then(load); });
                const drop = el("button", "btn small danger", "Remove");
                drop.type = "button";
                drop.addEventListener("click", function () {
                    if (confirm("Remove " + s.ref + " from this device? It was not recorded in the app, so it will be lost unless entered again.")) O.removeSale(s.key).then(load);
                });
                act.appendChild(retry);
                act.appendChild(drop);
            }
            if (s.kind === "planting") return [s.ref, when(s.made_at), s.userName, "Planting: " + s.summary, "\u2014", where, act];
            return [s.ref, when(s.made_at), s.userName, s.fields.customer_name || snap.business.walk_in, money(s.total), where, act];
        }), [0, 0, 0, 0, 1, 0, 0]));
        c.appendChild(el("p", "muted", "A sale the app turns down when it is uploaded (a product no longer on the POS, say) waits here with the reason. Put it right in the app, then try again."));
    }

    const SCREENS = { sell: showSell, stock: showStock, customers: showCustomers, invoices: showInvoices, plantings: showPlantings, waiting: showWaiting };

    function show() {
        tabs.forEach(function (b) { b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false"); b.classList.toggle("active", b.dataset.tab === tab); });
        view.replaceChildren();
        paintHead();
        if (!snap && tab !== "waiting") return;
        SCREENS[tab]();
    }

    function load() {
        return Promise.all([O.snapshot(), O.sales()]).then(function (got) {
            snap = got[0];
            all = got[1];
            show();
        });
    }

    tabs.forEach(function (b) {
        b.addEventListener("click", function () {
            tab = b.dataset.tab;
            history.replaceState(null, "", "#" + tab);
            show();
        });
    });
    // The till opens unless another screen was asked for (#stock, #waiting...)
    const asked = location.hash.slice(1);
    if (SCREENS[asked]) tab = asked;
    // A link to another screen (#waiting, from the pill) opens it, even when this page is already open
    window.addEventListener("hashchange", function () {
        const wanted = location.hash.slice(1);
        if (SCREENS[wanted] && wanted !== tab) { tab = wanted; show(); }
    });
    // A change in the background repaints the screen, unless a form on it is being filled in
    O.onChange(function () {
        if (document.querySelector(".offline-sale-form") && cart.length) return;
        if (document.querySelector(".offline-plant-form")) return;
        load();
    });
    window.addEventListener("online", load);
    window.addEventListener("offline", load);
    load();
})();
