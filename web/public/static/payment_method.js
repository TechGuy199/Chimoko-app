// A payment method is chosen from a list, and every method needs a reference number: for cash it is the receipt number.
// Picking a method renames the reference box to match (receipt number, cheque number, transaction ID...).
(function () {
    document.querySelectorAll("select[data-method-select]").forEach(function (select) {
        const scope = select.closest("form") || document;
        const reference = scope.querySelector("[data-reference]");
        const text = scope.querySelector("[data-reference-text]");
        if (!reference) return;

        function update() {
            const option = select.options[select.selectedIndex];
            const label = option && option.dataset.referenceLabel ? option.dataset.referenceLabel : "Reference number";
            if (text) text.textContent = label;
            reference.placeholder = select.value ? label : "";
            // Browsers enforce it where the form is not otherwise optional; the server checks it again
            if (select.dataset.requireReference !== undefined) reference.required = !!select.value;
        }
        select.addEventListener("change", update);
        update();
    });
})();
