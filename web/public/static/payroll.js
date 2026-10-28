(function () {
    const form = document.getElementById("payslip-form");
    const month = document.getElementById("pay-month");
    const expected = document.getElementById("expected-days");
    const worked = document.getElementById("worked-days");
    const nonWorking = form.dataset.nonWorking.split(",");
    const employee = document.getElementById("payslip-employee");
    const basisRow = document.getElementById("basis-row");
    const basis = document.getElementById("pay-basis");
    const trayBox = document.getElementById("tray-pay");
    const traySummary = document.getElementById("tray-summary");
    const trayList = document.getElementById("tray-list");
    const daysFields = document.getElementById("days-fields");
    const currency = form.dataset.currency || "";
    let basisTouched = false;   // the person chose how to pay, so the trays found no longer decide it
    let lookup = 0;             // which look-up is current: the answer to an older one is ignored
    const dayNames = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];

    function expectedDays(value) {
        const match = /^(\d{4})-(\d{2})$/.exec(value);
        if (!match) return null;
        const year = parseInt(match[1], 10);
        const monthIndex = parseInt(match[2], 10) - 1;
        if (monthIndex < 0 || monthIndex > 11) return null;
        const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
        let count = 0;
        for (let day = 1; day <= daysInMonth; day++) {
            if (!nonWorking.includes(dayNames[new Date(year, monthIndex, day).getDay()])) count++;
        }
        return count;
    }

    function refresh() {
        const days = expectedDays(month.value);
        if (days === null) return;
        // Keep "days worked" in step unless it was already changed to something different
        const follow = worked.value === "" || worked.value === expected.value;
        expected.value = days;
        if (follow) worked.value = days;
    }

    // A casual worker paid for trays has no days to enter
    function applyBasis() {
        const trays = !basisRow.hidden && basis.value === "trays";
        daysFields.hidden = trays;
        trayBox.hidden = !trays;
        expected.disabled = worked.disabled = trays;
        expected.required = worked.required = !trays;
    }

    function money(value) {
        return currency + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function trayCount(value) {
        return (Math.round(value * 100) / 100).toLocaleString("en-US");
    }

    function lookUpTrays() {
        const option = employee.selectedOptions[0];
        const casual = !!option && option.dataset.type === "Casual Worker";
        basisRow.hidden = !casual;
        basis.disabled = !casual;
        if (!casual) { applyBasis(); return; }
        const mine = ++lookup;
        traySummary.textContent = "Looking up the trays planted...";
        trayList.textContent = "";
        applyBasis();
        fetch(form.dataset.traysUrl + "?employee_id=" + encodeURIComponent(employee.value) + "&month=" + encodeURIComponent(month.value),
              { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (mine !== lookup) return;
                if (data.error) { traySummary.textContent = data.error; return; }
                trayList.textContent = "";
                data.rows.forEach(function (row) {
                    const item = document.createElement("li");
                    item.textContent = row.planting + ": " + trayCount(row.trays) + " tray" + (row.trays === 1 ? "" : "s");
                    trayList.appendChild(item);
                });
                if (!data.trays) {
                    traySummary.textContent = "No unpaid trays up to this pay month.";
                } else if (!data.rate) {
                    traySummary.textContent = trayCount(data.trays) + " unpaid trays, but the pay for each tray is not set. Set it in HR settings first.";
                } else {
                    traySummary.textContent = trayCount(data.trays) + " tray" + (data.trays === 1 ? "" : "s") + " at " + money(data.rate) + " = " + money(data.pay) + " basic pay.";
                }
                if (!basisTouched) basis.value = data.trays > 0 ? "trays" : "days";
                applyBasis();
            })
            .catch(function () {
                if (mine !== lookup) return;
                traySummary.textContent = "The trays could not be looked up. Pay by days worked, or try again.";
            });
    }

    basis.addEventListener("change", function () { basisTouched = true; applyBasis(); });
    employee.addEventListener("change", function () { basisTouched = false; lookUpTrays(); });
    month.addEventListener("input", refresh);
    month.addEventListener("change", function () { refresh(); lookUpTrays(); });
    refresh();
    lookUpTrays();

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
