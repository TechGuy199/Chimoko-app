// The assistant: a button on every page that opens a chat panel (Gemini, run by assistant.py). You can type or speak, and it can read its
// answers aloud. When it prepares a sale, pre-order, payment or delivery change, a card with Confirm and Cancel is shown: nothing happens
// until Confirm. Loaded once (base.html), so the panel stays open while the pages change underneath it.
(function () {
    "use strict";

    const cfg = document.currentScript.dataset;
    const MAX_SECONDS = 30;
    const RATE = 16000;
    let panel = null;     // the dialog, built on first open
    let ui = {};
    let ready = false;    // the recent chat has been loaded
    let busy = false;
    let recording = null;
    const player = new Audio();   // one element for every answer read aloud, so a phone that allows it once allows it always
    let unlocked = false;

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

    function icon(path, label) {
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

    function status(text) {
        ui.status.hidden = !text;
        ui.status.textContent = text || "";
        if (text) ui.status.appendChild(el("span", "dots"));
    }

    // ---------- Messages ----------
    function addMessage(role, text, extra) {
        const node = el("div", "msg msg-" + role);
        node.appendChild(words(text));
        if (role === "assistant" && !(extra && extra.plain)) {
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
    // The server gives up on a slow question well before this, so it is the answer that arrives. This is for a connection that has gone
    // quiet, so the panel never sits on "Thinking" for ever (data-timeout, in milliseconds, is there for testing).
    function send(url, form) {
        form.append("csrf", cfg.csrf);
        const stop = new AbortController();
        const timer = setTimeout(function () { stop.abort(); }, Number(cfg.timeout) || 100000);
        return fetch(url, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json" }, signal: stop.signal }).then(function (r) {
            clearTimeout(timer);
            if (r.redirected) { addError("You were signed out. Reload the page and sign in again."); return {}; }
            const kind = r.headers.get("content-type") || "";
            if (kind.indexOf("audio/") === 0) return r.blob().then(function (blob) { return { audio: blob }; });
            return r.json().then(function (data) {
                if (data.id && data.status) return { card: data };
                return data;
            }).catch(function () { return { error: "That did not work (" + r.status + "). Please try again." }; });
        }).catch(function (e) {
            clearTimeout(timer);
            if (e && e.name === "AbortError") return { error: "That is taking too long. Please try again in a minute." };
            return { error: "No connection. Check the internet and try again." };
        });
    }

    function ask(message, voice) {
        if (busy || !message.trim()) return;
        busy = true;
        unlock();
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

    // ---------- Speaking ----------
    // Phones only let a page play sound after a tap, so the shared player is started (silently) on the first tap
    function unlock() {
        if (unlocked) return;
        unlocked = true;
        player.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
        player.play().catch(function () { unlocked = false; });
    }

    function say(text) {
        player.pause();
        status("Getting the voice");
        const form = new FormData();
        form.append("text", text);
        send(cfg.speak, form).then(function (r) {
            status("");
            if (!r.audio) { if (r.error) addError(r.error); return; }
            player.src = URL.createObjectURL(r.audio);
            player.onended = function () { status(""); };
            player.play().catch(function () { addError("Tap the speaker under the answer to hear it."); });
        });
    }

    // ---------- Recording: raw sound, 16 kHz mono, made into a WAV file here, so every browser sends the same thing ----------
    function wav(chunks, fromRate) {
        let length = 0;
        chunks.forEach(function (c) { length += c.length; });
        const all = new Float32Array(length);
        let at = 0;
        chunks.forEach(function (c) { all.set(c, at); at += c.length; });
        const step = fromRate / RATE;
        const count = Math.floor(all.length / step);
        const buffer = new ArrayBuffer(44 + count * 2);
        const view = new DataView(buffer);
        const text = function (offset, s) { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
        text(0, "RIFF"); view.setUint32(4, 36 + count * 2, true); text(8, "WAVE"); text(12, "fmt ");
        view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
        view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
        text(36, "data"); view.setUint32(40, count * 2, true);
        for (let i = 0; i < count; i++) {
            // each new sample is the average of the old ones it covers, which keeps speech clear when the rate is lowered
            const from = Math.floor(i * step), to = Math.max(from + 1, Math.floor((i + 1) * step));
            let sum = 0;
            for (let j = from; j < to && j < all.length; j++) sum += all[j];
            const sample = Math.max(-1, Math.min(1, sum / (to - from)));
            view.setInt16(44 + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
        }
        return new Blob([buffer], { type: "audio/wav" });
    }

    function startRecording() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            addError("This browser cannot use the microphone here. Typing works.");
            return;
        }
        unlock();
        player.pause();
        navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }).then(function (stream) {
            const Context = window.AudioContext || window.webkitAudioContext;
            const context = new Context();
            const source = context.createMediaStreamSource(stream);
            const tap = context.createScriptProcessor(4096, 1, 1);
            const chunks = [];
            tap.onaudioprocess = function (e) { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
            source.connect(tap);
            tap.connect(context.destination);
            const began = Date.now();
            recording = { stream: stream, context: context, source: source, tap: tap, chunks: chunks, began: began, timer: null };
            ui.mic.classList.add("on");
            ui.mic.replaceChildren(icon(ICONS.stop));
            ui.mic.setAttribute("aria-label", "Stop and send");
            recording.timer = setInterval(function () {
                const seconds = Math.floor((Date.now() - began) / 1000);
                status("Listening " + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0") + " (tap the square to send)");
                if (seconds >= MAX_SECONDS) stopRecording(true);
            }, 250);
            status("Listening");
        }).catch(function () {
            addError("The microphone is not allowed or not available. Allow it for this site in the browser, or type instead.");
        });
    }

    function stopRecording(sendIt) {
        const r = recording;
        if (!r) return;
        recording = null;
        clearInterval(r.timer);
        r.tap.disconnect();
        r.source.disconnect();
        r.stream.getTracks().forEach(function (t) { t.stop(); });
        const rate = r.context.sampleRate;
        r.context.close();
        ui.mic.classList.remove("on");
        ui.mic.replaceChildren(icon(ICONS.mic));
        ui.mic.setAttribute("aria-label", "Speak");
        status("");
        if (!sendIt) return;
        busy = true;
        ui.send.disabled = ui.mic.disabled = true;
        status("Understanding");
        const form = new FormData();
        form.append("audio", wav(r.chunks, rate), "voice.wav");
        send(cfg.listen, form).then(function (reply) {
            busy = false;
            ui.send.disabled = ui.mic.disabled = false;
            status("");
            if (reply.error) { addError(reply.error); return; }
            ask(reply.text, true);
        });
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
        ui.speakToggle = button("assistant-toggle", "Read answers aloud", "speaker", function () {
            const on = keep("assistant.speak") !== "1";
            keep("assistant.speak", on ? "1" : "0");
            ui.speakToggle.setAttribute("aria-pressed", on ? "true" : "false");
            ui.speakToggle.classList.toggle("on", on);
            if (on) unlock(); else player.pause();
        });
        const on = keep("assistant.speak") === "1";
        ui.speakToggle.setAttribute("aria-pressed", on ? "true" : "false");
        ui.speakToggle.classList.toggle("on", on);
        tools.appendChild(ui.speakToggle);
        tools.appendChild(button("assistant-new", "Start a new chat", "again", function () {
            if (recording) stopRecording(false);
            player.pause();
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
        ui.mic = button("assistant-mic", "Speak", "mic", function () { if (recording) stopRecording(true); else if (!busy) startRecording(); });
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
            if (!s.configured) addError("The assistant is not connected to Gemini yet. An admin needs to add the Gemini key on the server.");
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
        if (recording) stopRecording(false);
        player.pause();
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
