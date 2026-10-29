// Importing planting records or salaries: before anything is saved, the file is sent to the server once to ask which names in it are not exactly the
// name of an employee. If there are any, this box asks, for each, who it is (or whether it is someone new); the answers go back with the file, which is
// then sent for real. Nothing is saved until the box is confirmed. The server asks again and never guesses, so this is only the conversation.
(function () {
    "use strict";
    const dialog = document.getElementById("name-check");
    if (!dialog || !dialog.showModal) return;
    const list = dialog.querySelector("[data-name-list]");
    const intro = dialog.querySelector("[data-name-intro]");
    const confirmButton = dialog.querySelector("[data-name-confirm]");
    let sending = null;   // the form whose file is being imported

    function make(tag, text, className) {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function option(group, value, label, checked) {
        const row = make("label", "", "check");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = group;
        radio.value = value;
        radio.checked = !!checked;
        row.appendChild(radio);
        row.appendChild(document.createTextNode(" " + label));
        return { row: row, radio: radio };
    }

    function describe(person) {
        const bits = [person.type, person.nrc ? "NRC " + person.nrc : ""].filter(Boolean);
        return person.name + (bits.length ? " (" + bits.join(", ") + ")" : "");
    }

    // One block for each name: who it might be, and the other things it could be
    function block(question, index, reply) {
        const group = "name-" + index;
        const planting = reply.kind === "planting";
        const box = make("div", "", "name-q");
        const rows = question.rows === 1 ? "1 row" : question.rows + " rows";
        box.appendChild(make("p", "", "")).append(make("strong", "\u201c" + question.name + "\u201d"), " is in " + rows + " of your file.");
        box.appendChild(make("p", question.how === "none"
            ? "No employee has that name."
            : question.how === "many" ? "More than one employee has that name."
            : "It is not exactly the name of an employee, but it is close to:", "muted"));
        const picks = [];
        question.candidates.forEach(function (person, i) {
            const o = option(group, "use:" + person.id, "Same person as " + describe(person), i === 0);
            picks.push(o);
            box.appendChild(o.row);
        });
        const other = option(group, "other", "Same person as another employee:", false);
        const select = document.createElement("select");
        select.setAttribute("aria-label", "Employee");
        reply.people.forEach(function (person) {
            const item = document.createElement("option");
            item.value = person.id;
            item.textContent = person.name;
            select.appendChild(item);
        });
        select.addEventListener("focus", function () { other.radio.checked = true; });
        other.row.appendChild(document.createTextNode(" "));
        other.row.appendChild(select);
        box.appendChild(other.row);
        const fresh = option(group, "new", planting ? "A different person: add them as a new casual worker" : "A different person: register them as a new employee",
                             question.how === "none");
        box.appendChild(fresh.row);
        if (planting) box.appendChild(option(group, "skip", "Leave their trays off (the trays stay in the batch, attached to nobody)", false).row);
        if (!question.candidates.length && question.how !== "none") fresh.radio.checked = true;
        box._answer = function () {
            const chosen = box.querySelector("input[name=" + group + "]:checked");
            if (!chosen) return null;
            return chosen.value === "other" ? "use:" + select.value : chosen.value;
        };
        return box;
    }

    function finish(form, answers) {
        form.elements.namedItem("name_choices").value = JSON.stringify(answers);
        form.dataset.confirmed = "1";
        form.submit();   // a real submission: the page that comes back is the import's own
    }

    function ask(form, reply) {
        list.textContent = "";
        intro.textContent = reply.questions.length === 1
            ? "One name in your file is not exactly the name of an employee. Say who it is, and the import will go ahead."
            : reply.questions.length + " names in your file are not exactly the names of employees. Say who each is, and the import will go ahead.";
        const blocks = reply.questions.map(function (question, i) { return block(question, i, reply); });
        blocks.forEach(function (b) { list.appendChild(b); });
        confirmButton.onclick = function () {
            const answers = {};
            reply.questions.forEach(function (question, i) {
                const answer = blocks[i]._answer();
                if (answer) answers[question.key] = answer;
            });
            dialog.close();
            finish(form, answers);
        };
        dialog.showModal();
    }

    function ready(form) {
        form.querySelectorAll("button:not([type=button])").forEach(function (b) { b.disabled = false; });
        sending = null;
    }

    // Cancel, Esc or a click outside the box: the file was not sent, so the Import button works again
    function cancelled() { if (sending && !sending.dataset.confirmed) ready(sending); }
    dialog.addEventListener("close", cancelled);
    dialog.querySelector("[data-modal-close]").addEventListener("click", cancelled);

    document.querySelectorAll("form[data-name-check]").forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (form.dataset.confirmed) return;   // the answers are in: send it for real
            event.preventDefault();
            event.stopImmediatePropagation();
            if (sending) return;
            sending = form;
            form.querySelectorAll("button:not([type=button])").forEach(function (b) { b.disabled = true; });
            const data = new FormData(form);
            data.set("check", "1");
            data.set("name_choices", "");
            fetch(form.action, { method: "POST", body: data, credentials: "same-origin", headers: { Accept: "application/json" } })
                .then(function (response) { return response.json(); })
                .then(function (reply) {
                    if (reply && reply.questions && reply.questions.length) ask(form, reply);
                    else finish(form, {});
                })
                .catch(function () { finish(form, {}); });   // no answer to the check: send the file, and the server says what it needs
        }, true);
    });
})();
