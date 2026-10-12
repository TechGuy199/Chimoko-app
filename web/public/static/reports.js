(function () {
    // Period picker: presets apply immediately, "Custom range" reveals the two date boxes
    const form = document.getElementById("report-filters");
    const period = document.getElementById("report-period");
    const custom = document.getElementById("custom-range");
    period.addEventListener("change", function () {
        if (period.value === "custom") {
            custom.hidden = false;
            custom.querySelector("input").focus();
        } else {
            form.submit();
        }
    });

    // One shared tooltip for every chart mark that carries data-tip
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    document.body.appendChild(tip);

    function place(e) {
        const pad = 14;
        const box = tip.getBoundingClientRect();
        let x = e.clientX + pad;
        let y = e.clientY + pad;
        if (x + box.width > window.innerWidth - 8) x = e.clientX - box.width - pad;
        if (y + box.height > window.innerHeight - 8) y = e.clientY - box.height - pad;
        tip.style.left = Math.max(8, x) + "px";
        tip.style.top = Math.max(8, y) + "px";
    }

    document.querySelectorAll("[data-tip]").forEach(function (mark) {
        mark.addEventListener("mouseenter", function (e) {
            tip.textContent = mark.dataset.tip;
            tip.hidden = false;
            place(e);
        });
        mark.addEventListener("mousemove", place);
        mark.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
})();
