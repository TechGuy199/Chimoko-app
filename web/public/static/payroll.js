(function () {
    // The payslip form: choosing the employee or the pay date asks the server what the payslip would pay (hr_trays: the period since
    // the last payslip, the unpaid trays at the worker's rate or the working days less absences, and the next pay date), and fills it in.
    // Changing the period asks again for those days. Days worked can still be corrected by hand.
    const form = document.getElementById("payslip-form");
    const employee = document.getElementById("payslip-employee");
    const payDate = document.getElementById("pay-date");
    const from = document.getElementById("period-start");
    const to = document.getElementById("period-end");
    const nextPay = document.getElementById("next-pay");
    const expected = document.getElementById("expected-days");
    const worked = document.getElementById("worked-days");
    const daysSummary = document.getElementById("days-summary");
    const basisRow = document.getElementById("basis-row");
    const basis = document.getElementById("pay-basis");
    const basisDays = document.getElementById("basis-days");
    const trayBox = document.getElementById("tray-pay");
    const traySummary = document.getElementById("tray-summary");
    const trayList = document.getElementById("tray-list");
    const daysFields = document.getElementById("days-fields");
    const currency = form.dataset.currency || "";
    let lookup = 0;   // which look-up is current: the answer to an older one is ignored

    function money(value) {
        return currency + Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function count(value) {
        return (Math.round(value * 100) / 100).toLocaleString("en-US");
    }

    // A casual worker is paid by the tray; one with a salary on record can be paid for days worked instead
    function applyBasis() {
        const option = employee.selectedOptions[0];
        const casual = !!option && option.dataset.type === "Casual Worker";
        const salary = option ? parseFloat(option.dataset.salary || "0") : 0;
        basisRow.hidden = !casual || !(salary > 0);
        basis.disabled = !casual;
        if (!casual) basis.value = "days";
        else if (!(salary > 0)) basis.value = "trays";
        basisDays.disabled = casual && !(salary > 0);
        const trays = casual && basis.value === "trays";
        daysFields.hidden = trays;
        trayBox.hidden = !trays;
        expected.disabled = worked.disabled = trays;
        expected.required = worked.required = !trays;
    }

    function ask(withPeriod) {
        if (!payDate.value) return;
        const mine = ++lookup;
        let url = form.dataset.traysUrl + "?employee_id=" + encodeURIComponent(employee.value) + "&pay_date=" + encodeURIComponent(payDate.value);
        if (withPeriod && from.value && to.value) url += "&from=" + encodeURIComponent(from.value) + "&to=" + encodeURIComponent(to.value);
        traySummary.textContent = "Working it out...";
        fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then(function (response) { return response.json(); })
            .then(function (d) {
                if (mine !== lookup) return;
                if (d.error) { traySummary.textContent = daysSummary.textContent = d.error; return; }
                from.value = d.start;
                to.value = d.end;
                if (!withPeriod) nextPay.value = d.next_pay_date || "";
                trayList.textContent = "";
                if (d.basis === "trays") {
                    (d.rows || []).forEach(function (row) {
                        const item = document.createElement("li");
                        item.textContent = row.planting + ": " + count(row.trays) + (row.kind ? " " + row.kind : "") + " tray" + (row.trays === 1 ? "" : "s") +
                            " \u00d7 " + money(row.rate) + " = " + money(row.amount);
                        trayList.appendChild(item);
                    });
                    if (!d.trays) traySummary.textContent = "No unpaid trays planted up to " + d.end + ".";
                    else if (d.unpriced) traySummary.textContent = count(d.trays) + " unpaid trays, but some have no pay set. Set the pay for each kind of tray under HR Settings (or this worker's own rate on their page) first.";
                    else traySummary.textContent = (d.kinds || []).map(function (k) {
                        return count(k.trays) + (k.name ? " " + k.name : "") + " \u00d7 " + money(k.rate);
                    }).join(" + ") + " = " + money(d.amount) + " basic pay.";
                } else {
                    expected.value = d.expected;
                    worked.value = d.worked;
                    daysSummary.textContent = d.expected + " working days from " + d.start + " to " + d.end +
                        (d.absent ? ", " + count(d.absent) + " recorded absent" : ", none recorded absent") + ": " + money(d.amount) + " basic pay.";
                }
                applyBasis();
            })
            .catch(function () {
                if (mine !== lookup) return;
                traySummary.textContent = daysSummary.textContent = "It could not be worked out just now. Fill in the days, or try again.";
            });
    }

    employee.addEventListener("change", function () {
        const option = employee.selectedOptions[0];
        if (option && option.dataset.next) payDate.value = option.dataset.next;
        applyBasis();
        ask(false);
    });
    payDate.addEventListener("change", function () { ask(false); });
    from.addEventListener("change", function () { ask(true); });
    to.addEventListener("change", function () { ask(true); });
    basis.addEventListener("change", applyBasis);
    applyBasis();
    ask(false);

    document.querySelectorAll("[data-add]").forEach(function (button) {
        button.addEventListener("click", function () {
            const kind = button.dataset.add;
            const row = document.createElement("div");
            row.className = "pair";

            const name = document.createElement("input");
            name.name = kind + "_name";
            name.placeholder = "Name";

            const amount = document.createElement("input");
            amount.name = kind + "_amount";
            amount.type = "number";
            amount.min = "0";
            amount.step = "0.01";
            amount.placeholder = "0.00";

            row.appendChild(name);
            row.appendChild(amount);
            document.getElementById(kind + "-rows").appendChild(row);
            name.focus();
        });
    });
})();
