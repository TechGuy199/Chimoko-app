// Merging categories or departments: tick two or more, name the result, and confirm.
// The tick-boxes can sit anywhere on the page; they belong to the merge form through their form="..." attribute.
(function () {
    document.querySelectorAll("form[data-merge]").forEach(function (form) {
        const noun = form.dataset.noun;
        const plural = form.dataset.plural;
        const boxes = Array.from(document.querySelectorAll('input[type="checkbox"][form="' + form.id + '"]'));
        const nameInput = form.querySelector('input[name$="_name"]');
        const button = form.querySelector("button");
        const count = form.querySelector(".merge-count");
        let typed = false;   // once the name has been typed by hand it is no longer filled in for you

        function ticked() {
            return boxes.filter(function (box) { return box.checked; });
        }

        function update() {
            const on = ticked();
            button.disabled = on.length < 2;
            count.textContent = on.length
                ? on.length + " " + (on.length === 1 ? noun : plural) + " ticked: " + on.map(function (box) { return box.dataset.name; }).join(", ")
                : "Nothing ticked yet.";
            // Suggest the first ticked name, which keeps that one and moves the others into it
            if (!typed) nameInput.value = on.length ? on[0].dataset.name : "";
        }

        nameInput.addEventListener("input", function () { typed = nameInput.value.trim() !== ""; });
        boxes.forEach(function (box) { box.addEventListener("change", update); });

        // The page-wide confirmation reads data-confirm when the form is submitted, so it is set just before that
        form.addEventListener("submit", function () {
            const names = ticked().map(function (box) { return "“" + box.dataset.name + "”"; }).join(", ");
            form.dataset.confirm = "Merge " + names + " into “" + nameInput.value.trim() + "”?\n\n" +
                "The " + plural + " you ticked are removed and everything in them moves to the merged " + noun + ". This cannot be undone.";
        });

        update();
    });
})();
