// The assistant: a button on every page that opens a chat panel (Claude, run by assistant.py). You can type or speak, and it can read its
// answers aloud, using the browser's own speech (so there is nothing to pay for and nothing to install). When it prepares a sale, pre-order,
// payment or delivery change, a card with Confirm and Cancel is shown: nothing happens until Confirm. Loaded once (base.html), so the
// panel stays open while the pages change underneath it.
(function () {
    "use strict";

    const cfg = document.currentScript.dataset;
    const MAX_SECONDS = 30;
    let panel = null;     // the dialog, built on first open
    let ui = {};
    let ready = false;    // the recent chat has been loaded
    let busy = false;
    let listening = null; // the speech recogniser while the microphone is open
    let primed = false;

    function keep(key, value) {
        try {
            if (value === undefined) return localStorage.getItem(key);
            localStorage.setItem(key, value);
        } catch (e) { /* private window: nothing is remembered, nothing breaks */ }
        return null;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function icon(path) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
        p.setAttribute("d", path);
        svg.appendChild(p);
        return svg;
    }

    const ICONS = {
        chat: "M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-8l-5 4v-4H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm4 5h8m-8 3h5",
        mic: "M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Zm-6 8a6 6 0 0 0 12 0m-6 6v3m-3 0h6",
        stop: "M7 7h10v10H7z",
        send: "M4 12 20 4l-4 16-4-6-8-2Zm8 2 4-6",
        speaker: "M4 9v6h4l5 4V5L8 9H4Zm12 0a4 4 0 0 1 0 6m2.5-9a8 8 0 0 1 0 12",
        close: "M6 6l12 12M18 6 6 18",
        again: "M4 12a8 8 0 0 1 14-5.3M20 4v5h-5M20 12a8 8 0 0 1-14 5.3M4 20v-5h5",
    };

    function button(className, label, iconName, handler) {
        const b = el("button", className);
        b.type = "button";
        b.setAttribute("aria-label", label);
        b.title = label;
        if (iconName) b.appendChild(icon(ICONS[iconName]));
        if (handler) b.addEventListener("click", handler);
        return b;
    }

    // The browser's own speech. Looked up when needed, not once, as some browsers only have it on a secure page.
    function recogniser() {
        return window.SpeechRecognition || window.webkitSpeechRecognition || null;
    }

    function canSpeak() {
        return !!window.speechSynthesis && typeof window.SpeechSynthesisUtterance === "function";
    }

    // ---------- Words on the screen: only text nodes are made from an answer, so nothing in it can ever be markup ----------
    function inline(node, text) {
        text.split(/(\*\*[^*]+\*\*)/g).forEach(function (part) {
            if (/^\*\*[^*]+\*\*$/.test(part)) node.appendChild(el("strong", "", part.slice(2, -2)));
            else if (part) node.appendChild(document.createTextNode(part));
        });
    }

    function words(text) {
        const box = el("div", "msg-text");
        let list = null;
        String(text).split("\n").forEach(function (line) {
            const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
            if (item) {
                if (!list) { list = el("ul"); box.appendChild(list); }
                const li = el("li");
                inline(li, item[1]);
                list.appendChild(li);
                return;
            }
            list = null;
            if (!line.trim()) return;
            const p = el("p");
            inline(p, line);
            box.appendChild(p);
        });
        return box;
    }

    function scrollDown() {
        ui.body.scrollTop = ui.body.scrollHeight;
    }

    function status(text, stoppable) {
        ui.status.hidden = !text;
        ui.status.replaceChildren();
        if (!text) return;
        ui.status.appendChild(document.createTextNode(text));
        if (stoppable) {
            const stop = el("button", "assistant-status-stop", "Stop");
            stop.type = "button";
            stop.addEventListener("click", stopSpeaking);
            ui.status.appendChild(stop);
        } else {
            ui.status.appendChild(el("span", "dots"));
        }
    }

    // ---------- Messages ----------
    function addMessage(role, text, extra) {
        const node = el("div", "msg msg-" + role);
        node.appendChild(words(text));
        if (role === "assistant" && !(extra && extra.plain) && canSpeak()) {
            const tools = el("div", "msg-tools");
            tools.appendChild(button("msg-listen", "Read this aloud", "speaker", function () { say(text); }));
            node.appendChild(tools);
        }
        if (extra && extra.sources && extra.sources.length) {
            const row = el("div", "msg-sources");
            extra.sources.forEach(function (s) {
                const a = el("a", "msg-source", s.label);
                a.href = s.url;
                a.addEventListener("click", function () { if (window.matchMedia("(max-width: 760px)").matches) closePanel(); });
                row.appendChild(a);
            });
            node.appendChild(row);
        }
        ui.body.appendChild(node);
        (extra && extra.actions || []).forEach(addCard);
        scrollDown();
        return node;
    }

    function addError(text) {
        const node = el("div", "msg msg-error");
        node.appendChild(el("p", "", text));
        ui.body.appendChild(node);
        scrollDown();
    }

    // A change the assistant prepared: its details, and Confirm / Cancel
    function addCard(card) {
        const node = el("section", "assistant-card");
        node.dataset.id = card.id;
        paintCard(node, card);
        ui.body.appendChild(node);
        scrollDown();
    }

    function paintCard(node, card) {
        node.replaceChildren();
        node.className = "assistant-card " + card.status;
        node.appendChild(el("h3", "", card.title));
        const list = el("dl");
        card.rows.forEach(function (row) {
            list.appendChild(el("dt", "", row[0]));
            list.appendChild(el("dd", "", row[1]));
        });
        node.appendChild(list);
        if (card.status === "pending") {
            const row = el("div", "assistant-card-buttons");
            const yes = el("button", "btn primary small", "Confirm");
            const no = el("button", "btn ghost small", "Cancel");
            yes.type = no.type = "button";
            yes.addEventListener("click", function () { decide(node, card, "confirm", yes, no); });
            no.addEventListener("click", function () { decide(node, card, "cancel", yes, no); });
            row.appendChild(yes);
            row.appendChild(no);
            node.appendChild(row);
            return;
        }
        const label = { done: "Done", failed: "Not done", cancelled: "Cancelled", expired: "Expired", running: "Working..." }[card.status] || card.status;
        node.appendChild(el("p", "assistant-card-result", label + (card.result ? ": " + card.result : "")));
        if (card.link) {
            const a = el("a", "btn small", "Open it");
            a.href = card.link;
            node.appendChild(a);
        }
    }

    function decide(node, card, what, yes, no) {
        yes.disabled = no.disabled = true;
        yes.textContent = what === "confirm" ? "Working..." : "Confirm";
        send(cfg.actions + card.id + "/" + what, new FormData()).then(function (r) {
            if (r.card) paintCard(node, r.card);
            else {
                addError(r.error || "That could not be done.");
                yes.disabled = no.disabled = false;
                yes.textContent = "Confirm";
            }
        });
    }

    // ---------- Talking to the server (a form post, so the app's CSRF check covers it) ----------
    function send(url, form) {
        form.append("csrf", cfg.csrf);
        return fetch(url, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) {
            if (r.redirected) { addError("You were signed out. Reload the page and sign in again."); return {}; }
            return r.json().then(function (data) {
                if (data.id && data.status) return { card: data };
                return data;
            }).catch(function () { return { error: "That did not work (" + r.status + "). Please try again." }; });
        }).catch(function () { return { error: "No connection. Check the internet and try again." }; });
    }

    function ask(message, voice) {
        if (busy || !message.trim()) return;
        busy = true;
        prime();
        stopSpeaking();
        addMessage("user", message);
        ui.input.value = "";
        grow();
        ui.send.disabled = ui.mic.disabled = true;
        status("Thinking");
        const form = new FormData();
        form.append("message", message);
        if (voice) form.append("voice", "1");
        send(cfg.chat, form).then(function (r) {
            status("");
            busy = false;
            ui.send.disabled = ui.mic.disabled = false;
            if (r.error) { addError(r.error); return; }
            if (!r.reply) return;
            addMessage("assistant", r.reply, { sources: r.sources, actions: r.actions });
            if (voice || keep("assistant.speak") === "1") say(r.reply);
        });
    }

    // ---------- Speaking: the browser reads the answer ----------
    // Phones only let a page speak after a tap, so the voice is started (silently) on the first one
    function prime() {
        if (primed || !canSpeak()) return;
        primed = true;
        try { window.speechSynthesis.speak(new SpeechSynthesisUtterance("")); } catch (e) { primed = false; }
    }

    // An answer without the marks that are for reading (bold, lists, links), in pieces a sentence or so long: some browsers stop reading
    // a long piece part-way through
    function spoken(text) {
        const plain = String(text)
            .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
            .replace(/https?:\/\/\S+/g, "")
            .replace(/^\s*(?:[-*•]|\d+[.)])\s+/gm, "")
            .replace(/[*_`#>|]+/g, "")
            .replace(/\s+/g, " ")
            .trim();
        const pieces = [];
        (plain.match(/[^.!?]+[.!?]*\s*/g) || []).forEach(function (sentence) {
            const last = pieces[pieces.length - 1];
            if (last && last.length + sentence.length < 180) pieces[pieces.length - 1] = last + sentence;
            else pieces.push(sentence);
        });
        return pieces;
    }

    function voiceFor(lang) {
        const voices = window.speechSynthesis.getVoices() || [];
        const same = voices.filter(function (v) { return v.lang && v.lang.toLowerCase().indexOf(lang.toLowerCase().slice(0, 2)) === 0; });
        return same.find(function (v) { return v.lang.toLowerCase() === lang.toLowerCase(); }) || same[0] || null;
    }

    function say(text) {
        if (!canSpeak()) return;
        stopSpeaking();
        const pieces = spoken(text);
        if (!pieces.length) return;
        const lang = navigator.language || "en-US";
        const voice = voiceFor(lang);
        pieces.forEach(function (piece, i) {
            const u = new SpeechSynthesisUtterance(piece);
            u.lang = lang;
            if (voice) u.voice = voice;
            if (i === pieces.length - 1) u.onend = u.onerror = function () { status(""); };
            window.speechSynthesis.speak(u);
        });
        status("Speaking", true);
    }

    function stopSpeaking() {
        if (canSpeak()) window.speechSynthesis.cancel();
        if (!busy && !listening) status("");
    }

    // ---------- Listening: the browser turns what is said into words, which show in the box as they come ----------
    function micIdle() {
        ui.mic.classList.remove("on");
        ui.mic.replaceChildren(icon(ICONS.mic));
        ui.mic.setAttribute("aria-label", "Speak");
        ui.mic.title = "Speak";
    }

    function startListening() {
        const Recognition = recogniser();
        if (!Recognition) return;
        prime();
        stopSpeaking();
        const heard = { words: "", failed: false };
        const r = new Recognition();
        r.lang = navigator.language || "en-US";
        r.interimResults = true;
        r.continuous = false;
        r.maxAlternatives = 1;
        r.onresult = function (e) {
            let said = "";
            for (let i = 0; i < e.results.length; i++) said += e.results[i][0].transcript;
            heard.words = said.trim();
            ui.input.value = heard.words;
            grow();
        };
        r.onerror = function (e) {
            heard.failed = true;
            const reasons = {
                "not-allowed": "The microphone is not allowed. Allow it for this site in the browser, or type instead.",
                "service-not-allowed": "The browser's speech service is not allowed here. Typing works.",
                "no-speech": "I did not hear anything. Tap the microphone and try again.",
                "audio-capture": "No microphone was found. Typing works.",
                "network": "Voice needs an internet connection. Typing works.",
            };
            if (e.error !== "aborted") addError(reasons[e.error] || "Voice did not work just now. Typing works.");
        };
        r.onend = function () {
            clearTimeout(listening && listening.timer);
            listening = null;
            micIdle();
            status("");
            if (!heard.failed && heard.words) ask(heard.words, true);
            else if (!heard.failed) addError("I did not catch that. Tap the microphone and try again.");
        };
        try {
            r.start();
        } catch (e) {
            addError("Voice could not start. Typing works.");
            return;
        }
        listening = { recogniser: r, timer: setTimeout(function () { r.stop(); }, MAX_SECONDS * 1000) };
        ui.mic.classList.add("on");
        ui.mic.replaceChildren(icon(ICONS.stop));
        ui.mic.setAttribute("aria-label", "Stop and send");
        ui.mic.title = "Stop and send";
        status("Listening, tap the square when you have finished");
    }

    function stopListening() {
        if (listening) listening.recogniser.stop();
    }

    function cancelListening() {
        if (!listening) return;
        const r = listening.recogniser;
        r.onend = null;
        clearTimeout(listening.timer);
        listening = null;
        r.abort();
        micIdle();
        status("");
    }

    // ---------- The panel ----------
    function grow() {
        ui.input.style.height = "auto";
        ui.input.style.height = Math.min(ui.input.scrollHeight, 120) + "px";
    }

    function build() {
        panel = el("dialog", "assistant");
        panel.setAttribute("aria-label", "Assistant");
        panel.dataset.busy = "1";   // dialog.js closes a dialog when its dark backdrop is clicked; a chat panel has none, so a stray click must not close it
        const head = el("header", "assistant-head");
        head.appendChild(el("strong", "", "Assistant"));
        const tools = el("span", "assistant-head-tools");
        if (canSpeak()) {
            ui.speakToggle = button("assistant-toggle", "Read answers aloud", "speaker", function () {
                const on = keep("assistant.speak") !== "1";
                keep("assistant.speak", on ? "1" : "0");
                ui.speakToggle.setAttribute("aria-pressed", on ? "true" : "false");
                ui.speakToggle.classList.toggle("on", on);
                if (on) prime(); else stopSpeaking();
            });
            const on = keep("assistant.speak") === "1";
            ui.speakToggle.setAttribute("aria-pressed", on ? "true" : "false");
            ui.speakToggle.classList.toggle("on", on);
            tools.appendChild(ui.speakToggle);
        }
        tools.appendChild(button("assistant-new", "Start a new chat", "again", function () {
            cancelListening();
            stopSpeaking();
            send(cfg.clear, new FormData()).then(function () { ui.body.replaceChildren(); welcome(); });
        }));
        tools.appendChild(button("assistant-close", "Close the assistant", "close", closePanel));
        head.appendChild(tools);
        panel.appendChild(head);

        ui.body = el("div", "assistant-body");
        ui.body.setAttribute("role", "log");
        ui.body.setAttribute("aria-live", "polite");
        panel.appendChild(ui.body);
        ui.status = el("p", "assistant-status");
        ui.status.hidden = true;
        panel.appendChild(ui.status);

        // Not a <form>: dialog.js closes any dialog whose form is submitted, which is right for an edit overlay and wrong for a chat
        const form = el("div", "assistant-form");
        ui.mic = button("assistant-mic", "Speak", "mic", function () { if (listening) stopListening(); else if (!busy) startListening(); });
        if (!recogniser()) {   // Firefox has no speech recognition: the microphone is not shown rather than shown and useless
            ui.mic.hidden = true;
        }
        ui.input = el("textarea");
        ui.input.rows = 1;
        ui.input.maxLength = 1000;
        ui.input.placeholder = "Ask about your business";
        ui.input.setAttribute("aria-label", "Your question");
        ui.input.addEventListener("input", grow);
        ui.input.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(ui.input.value, false); }
        });
        ui.send = button("assistant-send", "Send", "send", function () { ask(ui.input.value, false); });
        form.appendChild(ui.mic);
        form.appendChild(ui.input);
        form.appendChild(ui.send);
        panel.appendChild(form);

        panel.addEventListener("keydown", function (e) { if (e.key === "Escape") closePanel(); });
        // On <html>, not <body>: nav.js swaps the whole body on every page change, which would wipe the chat (the progress bar does the same)
        document.documentElement.appendChild(panel);
    }

    function welcome() {
        addMessage("assistant", "Hello " + cfg.user + ". Ask me about sales, stock, customers, deliveries or how to do something in the app. I can also prepare a sale or a pre-order for you to confirm.", { plain: true });
    }

    function load() {
        status("Opening");
        fetch(cfg.state, { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) { return r.json(); }).then(function (s) {
            status("");
            ready = true;
            if (!s.enabled) { addError("The assistant is switched off for this business." + (s.can_enable ? " You can switch it on in Settings." : " Ask an admin to switch it on.")); return; }
            if (!s.configured) addError("The assistant is not connected to Claude yet. An admin needs to add the Claude key on the server.");
            if (!s.messages.length && !s.actions.length) welcome();
            s.messages.forEach(function (m) { addMessage(m.role, m.content, { plain: m.role === "assistant" && /^(Confirmed|Tried to confirm|Cancelled):/.test(m.content) }); });
            s.actions.forEach(addCard);
            scrollDown();
        }).catch(function () { status(""); addError("Could not open the assistant. Check the connection and try again."); });
    }

    function openPanel() {
        if (!panel) build();
        if (!panel.open) panel.show();
        fab.classList.add("open");
        fab.setAttribute("aria-expanded", "true");
        if (!ready) load();
        setTimeout(function () { ui.input.focus(); }, 50);
    }

    function closePanel() {
        cancelListening();
        stopSpeaking();
        if (panel && panel.open) panel.close();
        fab.classList.remove("open");
        fab.setAttribute("aria-expanded", "false");
    }

    const fab = button("assistant-fab no-print", "Ask the assistant", "chat", function () {
        if (panel && panel.open) closePanel(); else openPanel();
    });
    fab.setAttribute("aria-expanded", "false");
    document.documentElement.appendChild(fab);
})();
