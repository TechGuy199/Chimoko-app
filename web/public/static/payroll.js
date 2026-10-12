(function () {
    const form = document.getElementById("payslip-form");
    const month = document.getElementById("pay-month");
    const expected = document.getElementById("expected-days");
    const worked = document.getElementById("worked-days");
    const nonWorking = form.dataset.nonWorking.split(",");
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

    month.addEventListener("input", refresh);
    month.addEventListener("change", refresh);
    refresh();

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
