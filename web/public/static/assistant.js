// The assistant: a button on every page that opens a chat panel (Gemini, run by assistant.py). You can type or speak, send a photo or a PDF
// (a receipt, a tally of trays), and it can read its answers aloud. When it prepares a change, a card with Confirm and Cancel is shown:
// nothing happens until Confirm. A message it writes for a customer comes with Copy and Open in WhatsApp; it never sends anything itself.
// In the evening it gives a brief without being asked. With Listen on (the browser's own speech recognition picks out the phrases), the
// business's wake phrase and a question ask it out loud, and it answers in the voice the person chose; hands-free mode is a spoken
// conversation, with changes confirmed by saying yes or no. While it listens, a tap outside shrinks the panel to a small one that still listens.
// Loaded once (base.html), so the panel stays open while the pages change underneath it.
(function () {
    "use strict";

    const cfg = document.currentScript.dataset;
    const MAX_SECONDS = 30;
    const MAX_FILE = 1900000;   // what the server takes in one question (the app's upload limit is 2 MB)
    const PHOTO_SIDE = 1600;    // a photo is shrunk to this many pixels on its long side: plenty to read a receipt, and quick to send
    const RATE = 16000;
    let panel = null;     // the dialog, built on first open
    let ui = {};
    let ready = false;    // the recent chat has been loaded
    let busy = false;
    let recording = null;
    const player = new Audio();   // one element for every answer read aloud, so a phone that allows it once allows it always
    let unlocked = false;
    let reading = 0;              // which reading aloud is current: a newer one, or Stop, makes an older one give up
    let preparing = false;        // the status line says the voice is being made
    let attachment = null;        // the photo or PDF going with the next question: {blob, name, kind}
    let voices = [];              // the voices a person can choose from: [{name, style}] (cfg, the script's data, holds only text)

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
        camera: "M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Zm8 3a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z",
        today: "M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
        ear: "M7 10a5 5 0 0 1 10 0c0 3-3 4-3 7a3 3 0 0 1-6 0m2-7a2 2 0 0 1 4 0",
        memory: "M7 4h10a1 1 0 0 1 1 1v15l-6-4-6 4V5a1 1 0 0 1 1-1Z",
        free: "M4 15v-3a8 8 0 0 1 16 0v3M4 15a2 2 0 0 1 2-2h1v6H6a2 2 0 0 1-2-2v-2Zm16 0a2 2 0 0 0-2-2h-1v6h1a2 2 0 0 0 2-2v-2Z",
        more: "M5 12h.01M12 12h.01M19 12h.01",
        shrink: "M6 14h4v4M18 10h-4V6M10 14l-6 6M14 10l6-6",
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
        (extra && extra.drafts || []).forEach(addDraft);
        (extra && extra.notes || []).forEach(function (note) { ui.body.appendChild(el("p", "msg-note", note)); });
        scrollDown();
        return node;
    }

    // A message the assistant wrote for the person to send a customer: Copy, or open WhatsApp with it filled in. Nothing is sent from here.
    function addDraft(draft) {
        const node = el("section", "assistant-card assistant-draft");
        node.appendChild(el("h3", "", draft.title));
        node.appendChild(el("p", "assistant-draft-text", draft.text));
        const row = el("div", "assistant-card-buttons");
        const copy = el("button", "btn small", "Copy");
        copy.type = "button";
        copy.addEventListener("click", function () {
            const done = function () { copy.textContent = "Copied"; setTimeout(function () { copy.textContent = "Copy"; }, 1500); };
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(draft.text).then(done, function () { addError("Copy did not work: select the text and copy it."); });
            else addError("Copy did not work: select the text and copy it.");
        });
        row.appendChild(copy);
        const wa = el("a", "btn small primary", draft.phone ? "Open in WhatsApp" : "Open in WhatsApp (choose the chat)");
        wa.href = draft.whatsapp;
        wa.target = "_blank";
        wa.rel = "noopener";
        row.appendChild(wa);
        node.appendChild(row);
        ui.body.appendChild(node);
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
        waiting = waiting.filter(function (w) { return w.node !== node; });
        act(node, card, what).then(function (done) {
            if (!done) {
                yes.disabled = no.disabled = false;
                yes.textContent = "Confirm";
            }
            paintEar();
        });
    }

    // Confirm or Cancel a card, by its buttons or by voice: the card as it ends up, or null (the error is shown)
    function act(node, card, what) {
        return send(cfg.actions + card.id + "/" + what, new FormData()).then(function (r) {
            if (r.card) { paintCard(node, r.card); return r.card; }
            addError(r.error || "That could not be done.");
            return null;
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

    // opts.heard: the question was spoken while listening, so the answer is spoken back (and a change it prepared can be confirmed by voice);
    // opts.handsfree: in a hands-free conversation
    function ask(message, voice, opts) {
        const heard = !!(opts && opts.heard);
        if (busy || (!message.trim() && !attachment)) return;
        busy = true;
        unlock();
        const sending = attachment;
        const said = addMessage("user", message.trim() || (sending.kind === "pdf" ? "(PDF)" : "(photo)"));
        if (sending) {
            if (sending.kind === "photo") {
                const img = el("img", "msg-photo");
                img.alt = "The photo sent";
                img.src = URL.createObjectURL(sending.blob);
                said.appendChild(img);
            } else said.appendChild(el("p", "msg-file", "PDF: " + sending.name));
        }
        clearAttachment();
        ui.input.value = "";
        grow();
        ui.send.disabled = ui.mic.disabled = ui.photo.disabled = true;
        status(sending ? "Reading it" : "Thinking");
        paintEar();
        const form = new FormData();
        form.append("message", message);
        if (voice) form.append("voice", "1");
        if (opts && opts.handsfree) form.append("handsfree", "1");
        if (sending) form.append("attachment", sending.blob, sending.name);
        send(cfg.chat, form).then(function (r) {
            status("");
            busy = false;
            ui.send.disabled = ui.mic.disabled = ui.photo.disabled = false;
            paintEar();
            if (r.error) { addError(r.error); if (heard) talkBack(r.error); return; }
            if (!r.reply) return;
            addMessage("assistant", r.reply, { sources: r.sources, actions: r.actions, drafts: r.drafts, notes: r.notes });
            if (heard) {
                // A change prepared while talking waits for a spoken yes or no (or the card's own buttons)
                waiting = (r.actions || []).filter(function (a) { return a.status === "pending"; }).map(function (card) {
                    return { card: card, node: ui.body.querySelector('.assistant-card[data-id="' + card.id + '"]') };
                });
                talkBack(r.reply + (waiting.length && !/\byes\b/i.test(r.reply) ? " Say yes to confirm, or no to cancel." : ""));
            } else if (voice || keep("assistant.speak") === "1") say(r.reply);
        });
    }

    // ---------- The evening brief: given once a day from the brief hour, without being asked ----------
    function giveBrief() {
        if (busy) return;
        busy = true;
        ui.send.disabled = ui.mic.disabled = ui.photo.disabled = true;
        status("Writing your evening brief");
        send(cfg.brief, new FormData()).then(function (r) {
            status("");
            busy = false;
            ui.send.disabled = ui.mic.disabled = ui.photo.disabled = false;
            briefBadge(false);
            if (r.error) { if (!/given already/.test(r.error)) addError(r.error); return; }
            if (!r.reply) return;
            addMessage("user", "Evening brief");
            addMessage("assistant", r.reply, { sources: r.sources, actions: r.actions, drafts: r.drafts, notes: r.notes });
            if (ear) talkBack(r.reply);
            else if (keep("assistant.speak") === "1") say(r.reply);
        });
    }

    function briefBadge(on) {
        fab.classList.toggle("has-brief", on);
        if (bubble) bubble.hidden = !on;
    }

    // ---------- What it remembers: each note can be forgotten here, and the evening brief switched off for oneself ----------
    function showMemory() {
        if (ui.memory && ui.memory.isConnected) { ui.memory.remove(); ui.memory = null; return; }
        const box = el("section", "assistant-card assistant-memory");
        ui.memory = box;
        box.appendChild(el("h3", "", "What I remember"));
        box.appendChild(el("p", "msg-note", "Opening"));
        ui.body.appendChild(box);
        scrollDown();
        fetch(cfg.memory, { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) { return r.json(); }).then(function (m) {
            box.replaceChildren(el("h3", "", "What I remember"));
            if (m.error) { box.appendChild(el("p", "", m.error)); return; }
            const list = el("ul", "assistant-memory-list");
            function line(text, label, url, can) {
                const li = el("li");
                li.appendChild(el("span", "", text));
                if (label) li.appendChild(el("small", "", label));
                if (can) {
                    const b = el("button", "btn ghost small", "Forget");
                    b.type = "button";
                    b.addEventListener("click", function () {
                        b.disabled = true;
                        send(url, new FormData()).then(function (r) { if (r.error) { b.disabled = false; addError(r.error); } else li.remove(); });
                    });
                    li.appendChild(b);
                }
                list.appendChild(li);
            }
            m.notes.forEach(function (n) { line(n.note, n.everyone ? "Everyone" : "Just you", cfg.memory + "/" + n.id + "/delete", n.can_delete); });
            m.stock.forEach(function (s) { line(s.item + ": " + s.setting, "Restocking", cfg.stockPrefs + s.id + "/delete", s.can_delete); });
            if (list.children.length) box.appendChild(list);
            else box.appendChild(el("p", "", "Nothing yet. Tell me a preference, like \"keep answers short\" or \"keep six weeks of fertiliser\", and I will offer to remember it."));
            box.appendChild(el("p", "msg-note", "Stock is bought to last " + m.keep_days + " days, unless an item says otherwise."));
            const label = el("label", "check");
            const tick = el("input");
            tick.type = "checkbox";
            tick.checked = m.brief.mine;
            tick.disabled = !m.brief.business;
            tick.addEventListener("change", function () {
                const form = new FormData();
                form.append("on", tick.checked ? "1" : "0");
                send(cfg.myBrief, form).then(function (r) { if (r.error) addError(r.error); });
            });
            label.appendChild(tick);
            label.appendChild(el("span", "", m.brief.business ? " Give me the evening brief from " + m.brief.hour + ":00" : " The evening brief is switched off for the business (Settings)."));
            box.appendChild(label);
            scrollDown();
        }).catch(function () { box.appendChild(el("p", "", "Could not open it. Check the connection.")); });
    }

    // ---------- The voice it speaks with: each person's own, kept until they choose another ----------
    function showVoices() {
        if (ui.voiceCard && ui.voiceCard.isConnected) { ui.voiceCard.remove(); ui.voiceCard = null; return; }
        const box = el("section", "assistant-card assistant-voices");
        ui.voiceCard = box;
        box.appendChild(el("h3", "", "My voice"));
        const pickList = el("select");
        pickList.setAttribute("aria-label", "Voice");
        voices.forEach(function (v) {
            const option = el("option", "", v.name + " — " + v.style.toLowerCase());
            option.value = v.name;
            option.selected = v.name === cfg.voice;
            pickList.appendChild(option);
        });
        box.appendChild(pickList);
        const row = el("div", "assistant-card-buttons");
        const hear = el("button", "btn small", "Hear it");
        hear.type = "button";
        hear.addEventListener("click", function () { unlock(); say("Hello, " + cfg.user + ". This is how I would sound when I answer you.", null, pickList.value); });
        const use = el("button", "btn small primary", "Use this voice");
        use.type = "button";
        use.addEventListener("click", function () {
            const form = new FormData();
            form.append("voice", pickList.value);
            use.disabled = true;
            send(cfg.voiceUrl, form).then(function (r) {
                use.disabled = false;
                if (r.error) { addError(r.error); return; }
                cfg.voice = r.voice;
                box.remove();
                ui.body.appendChild(el("p", "msg-note", "I will speak with " + r.voice + " from now on, until you choose another."));
                scrollDown();
            });
        });
        row.appendChild(hear);
        row.appendChild(use);
        box.appendChild(row);
        box.appendChild(el("p", "msg-note", "Used for every spoken answer, on any device you sign in on."));
        ui.body.appendChild(box);
        scrollDown();
    }

    // ---------- Listening: the browser's own speech recognition picks out the phrases; Gemini answers, in the voice the person chose ----------
    // Listen (the ear): the wake phrase and a question ("Hey Mark, what is low?") ask it; the wake phrase alone waits a few seconds for the
    // question, and after an answer a follow-up is listened for without it. Hands-free: no wake phrase, a spoken conversation, and a change it
    // prepares is confirmed or cancelled by saying yes or no. The stop phrase switches listening off, even while the panel is small. While it
    // speaks, only the wake or stop phrase is taken, so its own voice is never taken for a question.
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const AWAKE_MS = 8000;
    const LANGS = ["en-US", "en-GB", "en-ZA", "en-KE", "en-NG", "en-IN", "en-AU", "en-CA", "en-IE", "en-NZ", "en-PH", "en-TZ", "en-GH"];
    const SPEECH_LANG = LANGS.indexOf(navigator.language) >= 0 ? navigator.language : "en-GB";
    const GREETINGS = ["hey", "hi", "hello", "ok", "okay"];
    const YES = /^(yes|yeah|yep|yup|sure|ok|okay|alright|confirm|confirmed|do it|go ahead|please do|correct|that'?s right)\b/;
    const NO = /^(no|nope|cancel|don'?t|do not|leave it|never mind|not now)\b/;
    cfg.wake = cfg.wake || "Hey Assistant";
    cfg.stop = cfg.stop || "Stop listening";
    let ear = null;          // {rec, awake, timer, fails, started} while listening
    let handsfree = false;   // a spoken conversation, no wake phrase
    let talking = false;     // it is speaking an answer
    let hearing = null;      // the person is speaking: the wave moves (a timer that stops it when they pause)
    let heardSoFar = "";
    let waiting = [];        // changes from the last spoken answer, to confirm or cancel by voice: [{card, node}]
    let resumeEar = false;   // listening paused for a recording with the microphone button
    let chimes = null;

    function plain(text) {
        return String(text).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
    }

    // What was said around a phrase, or null when it was not said. One that starts with a greeting ("Hey Mark") is heard with any ("OK Mark").
    function around(text, phrase) {
        const t = " " + plain(text) + " ";
        const words = plain(phrase).split(" ");
        const tail = words.slice(GREETINGS.indexOf(words[0]) >= 0 && words.length > 1 ? 1 : 0).join(" ").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const found = new RegExp(" " + (tail !== words.join(" ") ? "(?:" + GREETINGS.join("|") + "|hay|a) " : "") + tail + " ").exec(t);
        if (!found) return null;
        return (t.slice(0, found.index) + " " + t.slice(found.index + found[0].length)).trim();
    }

    function chime() {
        try {
            if (!chimes) chimes = new (window.AudioContext || window.webkitAudioContext)();
            const tone = chimes.createOscillator(), gain = chimes.createGain();
            tone.frequency.value = 880;
            gain.gain.setValueAtTime(0.08, chimes.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, chimes.currentTime + 0.18);
            tone.connect(gain);
            gain.connect(chimes.destination);
            tone.start();
            tone.stop(chimes.currentTime + 0.2);
        } catch (e) { /* no sound: the words on the screen still say it is listening */ }
    }

    function wave() {
        const w = el("span", "assistant-wave");
        w.setAttribute("aria-hidden", "true");
        for (let i = 0; i < 5; i++) w.appendChild(el("i"));
        return w;
    }

    // The listening bar in the panel and the small panel show where things stand; the wave moves while the person speaks and while it answers
    function paintEar() {
        if (!ui.ear) return;
        ui.ear.classList.toggle("on", !!ear);
        ui.ear.setAttribute("aria-pressed", ear ? "true" : "false");
        ui.free.classList.toggle("on", handsfree);
        ui.free.setAttribute("aria-pressed", handsfree ? "true" : "false");
        const state = !ear ? "" : talking ? "speaking" : busy ? "thinking" : hearing ? "hearing" : "listening";
        let text = "";
        if (ear) {
            if (hearing && heardSoFar) text = heardSoFar;
            else if (talking) text = "Speaking. Say “" + cfg.wake + "” to interrupt";
            else if (busy) text = "Thinking";
            else if (waiting.length) text = "Say yes to confirm, or no to cancel";
            else if (handsfree) text = "Hands-free: just talk to me";
            else if (ear.awake) text = "Listening: say your question";
            else text = "Say “" + cfg.wake + "” and your question";
        }
        ui.listenBar.hidden = !ear;
        ui.listenBar.dataset.voice = state;
        ui.listenText.textContent = text;
        ui.mini.dataset.voice = state;
        ui.miniText.textContent = text || "Assistant";
        // Listening switched off (the stop phrase) while small: the small panel goes, leaving the button, without popping the chat up
        if (!ear && panel && panel.classList.contains("mini")) {
            panel.classList.remove("mini");
            ui.mini.hidden = true;
            if (panel.open) panel.close();
            fab.hidden = false;
            fab.classList.remove("open");
            fab.setAttribute("aria-expanded", "false");
        }
    }

    function startListening() {
        if (!Recognition || ear || recording) return;
        const rec = new Recognition();
        rec.continuous = true;
        rec.interimResults = true;
        rec.lang = SPEECH_LANG;
        ear = { rec: rec, awake: false, timer: null, fails: 0, started: 0 };
        rec.onresult = function (e) { if (ear && ear.rec === rec) hear(e); };
        rec.onerror = function (e) {
            if (e.error === "not-allowed" || e.error === "service-not-allowed") {
                stopListening(false);
                addError("The microphone is not allowed for this site, so listening is off. Allow it in the browser to use it.");
            }
        };
        rec.onend = function () {
            if (!ear || ear.rec !== rec) return;
            // The browser stops now and then (on a phone after every phrase): it is started again while listening is on, unless it keeps failing
            ear.fails = Date.now() - ear.started < 1500 ? ear.fails + 1 : 0;
            if (ear.fails >= 5) {
                stopListening(false);
                addError("Listening keeps stopping. This browser's speech recognition needs the internet; turn Listen on again to try again.");
                return;
            }
            setTimeout(function () { if (ear && ear.rec === rec) begin(); }, 250);
        };
        begin();
        keep("assistant.listen", "1");
        if (keep("assistant.listen.told") !== "1") {
            keep("assistant.listen.told", "1");
            addMessage("assistant", "Listening is on. Say “" + cfg.wake + "” and your question, and I will answer out loud; say “" + cfg.stop + "” to stop me listening. " +
                "Tap outside this panel and it shrinks, still listening. While it listens, this browser sends what the microphone hears to its speech service " +
                "(Google's, in Chrome) to pick out the phrases.", { plain: true });
        }
        paintEar();
    }

    function begin() {
        try { ear.started = Date.now(); ear.rec.start(); } catch (e) { /* already listening */ }
    }

    // keepChoice: paused (panel closed, page hidden, a recording), not switched off, so it starts again next time
    function stopListening(keepChoice) {
        const e = ear;
        ear = null;
        if (e) {
            clearTimeout(e.timer);
            e.rec.onend = null;
            try { e.rec.abort(); } catch (x) { /* already stopped */ }
        }
        if (!keepChoice) {
            keep("assistant.listen", "0");
            handsfree = false;
            keep("assistant.handsfree", "0");
            waiting = [];
        }
        paintEar();
    }

    function setHandsfree(on) {
        handsfree = on;
        keep("assistant.handsfree", on ? "1" : "0");
        if (on) {
            unlock();
            startListening();
            chime();
        }
        paintEar();
    }

    function wake() {
        if (!ear) return;
        ear.awake = true;
        clearTimeout(ear.timer);
        ear.timer = setTimeout(function () { if (ear) { ear.awake = false; paintEar(); } }, AWAKE_MS);
        chime();
        paintEar();
    }

    function hear(e) {
        let said = "", partly = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
            if (e.results[i].isFinal) said += e.results[i][0].transcript + " ";
            else partly += e.results[i][0].transcript;
        }
        if (partly && !talking) {
            heardSoFar = partly;
            clearTimeout(hearing);
            hearing = setTimeout(function () { hearing = null; heardSoFar = ""; paintEar(); }, 1400);
            paintEar();
        }
        said = said.trim();
        if (!said) return;
        clearTimeout(hearing);
        hearing = null;
        heardSoFar = "";
        if (around(said, cfg.stop) !== null) {
            stopSpeaking();
            stopListening(false);
            ui.body.appendChild(el("p", "msg-note", "Listening is off. Tap the ear to start it again."));
            scrollDown();
            return;
        }
        const rest = around(said, cfg.wake);
        if (talking) {
            if (rest === null) return;   // its own voice, or someone else: only the wake phrase interrupts it
            stopSpeaking();
        }
        if (busy) { paintEar(); return; }
        const words = plain(rest !== null ? rest : said);
        if (waiting.length && (handsfree || ear.awake || rest !== null)) {
            if (YES.test(words)) { decideByVoice("confirm"); return; }
            if (NO.test(words)) { decideByVoice("cancel"); return; }
        }
        if (handsfree) {
            if (words) question(rest !== null ? rest : said); else wake();
            return;
        }
        if (rest !== null && rest.split(" ").length >= 2) { question(rest); return; }
        if (rest !== null) { wake(); return; }
        if (ear.awake) question(said);
        else paintEar();
    }

    function question(text) {
        ear.awake = false;
        clearTimeout(ear.timer);
        ask(text.charAt(0).toUpperCase() + text.slice(1), true, { heard: true, handsfree: handsfree });
    }

    // A spoken yes or no to what was prepared: the cards are confirmed or cancelled as their own buttons would, and the outcome is said
    function decideByVoice(what) {
        const cards = waiting.filter(function (w) { return w.node && w.node.isConnected; });
        waiting = [];
        if (!cards.length) { paintEar(); return; }
        busy = true;
        paintEar();
        Promise.all(cards.map(function (w) { return act(w.node, w.card, what); })).then(function (done) {
            busy = false;
            const failed = done.filter(function (c) { return !c || c.status !== (what === "confirm" ? "done" : "cancelled"); });
            if (what === "cancel") talkBack(failed.length ? "I could not cancel that." : "Cancelled.");
            else if (!failed.length) talkBack("Done. " + ((done[0] && done[0].result) || "").split(/(?<=[.!?])\s/)[0]);
            else talkBack("That did not go through. " + ((failed[0] && failed[0].result) || "Look at the card for why."));
        });
    }

    // An answer to something heard is spoken back; then, listening for the wake phrase, a follow-up is waited for without it
    function talkBack(text) {
        say(text, function () { if (ear && !handsfree) wake(); paintEar(); });
    }

    // ---------- Speaking: Gemini's voice, the one the person chose ----------
    // Phones only let a page play sound after a tap, so the shared player is started (silently) on the first tap
    function unlock() {
        if (unlocked) return;
        unlocked = true;
        player.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
        player.play().catch(function () { unlocked = false; });
    }

    // Making speech takes time in proportion to its length, so a long answer is read in two pieces: the first sentence, which is ready
    // soon, and the rest. Both are asked for at once and the rest plays when the first sentence ends.
    function pieces(text) {
        const t = String(text).trim();
        if (t.length <= 160) return [t];
        const first = t.match(/^[\s\S]{15,200}?[.!?:](?=\s)/);
        if (!first || t.length - first[0].length < 40) return [t];
        return [first[0], t.slice(first[0].length).trim()];
    }

    function stopSpeaking() {
        reading++;
        player.pause();
        player.onended = null;
        if (window.speechSynthesis) window.speechSynthesis.cancel();
        if (preparing) { preparing = false; status(""); }
        if (talking) { talking = false; paintEar(); }
    }

    function stopTalking() {
        stopSpeaking();
    }

    // after: called once it has finished speaking (not when it is stopped). voice: another voice, to hear it before choosing it.
    function say(text, after, voice) {
        stopSpeaking();
        const mine = reading;
        talking = true;
        paintEar();
        function finish() {
            if (mine !== reading) return;
            talking = false;
            paintEar();
            if (after) after();
        }
        const parts = pieces(text);
        const clips = parts.map(function (part) {
            const form = new FormData();
            form.append("text", part);
            if (voice) form.append("voice", voice);
            return send(cfg.speak, form);
        });
        let at = 0;
        function next() {
            if (mine !== reading) return;
            if (at >= clips.length) { finish(); return; }
            preparing = true;
            status("Getting the voice");
            const part = at;
            clips[at++].then(function (r) {
                if (mine !== reading) return;
                preparing = false;
                status("");
                if (!r.audio) {
                    // No Gemini voice just now (Google busy, no connection): the browser's own reads the rest, so a conversation is not left hanging
                    if (r.error) addError(r.error);
                    browserVoice(parts.slice(part).join(" "), finish);
                    return;
                }
                const url = URL.createObjectURL(r.audio);
                player.src = url;
                player.onended = function () { URL.revokeObjectURL(url); next(); };
                player.play().catch(function () { addError("Tap the speaker under the answer to hear it."); finish(); });
            });
        }
        next();
    }

    function browserVoice(text, then) {
        if (!window.speechSynthesis) { then(); return; }
        const words = String(text).replace(/\*\*/g, "").replace(/^\s*(?:[-*•]|\d+[.)])\s+/gm, "").replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim().slice(0, 700);
        const lines = words.match(/[^.!?]+[.!?]*/g) || [words];
        lines.forEach(function (line, i) {
            const u = new SpeechSynthesisUtterance(line.trim());
            u.lang = SPEECH_LANG;
            if (i === lines.length - 1) u.onend = u.onerror = then;
            window.speechSynthesis.speak(u);
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
        stopSpeaking();
        stopTalking();
        if (ear) { resumeEar = true; stopListening(true); }
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
            if (resumeEar) { resumeEar = false; startListening(); }
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
        if (resumeEar && panel && panel.open) { resumeEar = false; startListening(); }
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
    // ---------- A photo or PDF to send ----------
    // A photo is shrunk first (a phone camera's 4000-pixel photo becomes a few hundred kB), so it is quick to send and within the limit.
    // A format the browser cannot draw (HEIC on most computers) is sent as it is, if it is small enough.
    function shrink(file) {
        return new Promise(function (resolve) {
            const url = URL.createObjectURL(file);
            const img = new Image();
            img.onload = function () {
                const scale = Math.min(1, PHOTO_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
                const canvas = document.createElement("canvas");
                canvas.width = Math.round(img.naturalWidth * scale);
                canvas.height = Math.round(img.naturalHeight * scale);
                canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
                URL.revokeObjectURL(url);
                canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.85);
            };
            img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
            img.src = url;
        });
    }

    function pick(file) {
        if (!file) return;
        const pdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
        if (!pdf && file.type.indexOf("image/") !== 0 && !/\.(heic|heif)$/i.test(file.name)) { addError("Send a photo or a PDF."); return; }
        // Until it is ready, Send waits: otherwise a quick tap would send the words without the photo
        ui.send.disabled = true;
        status("Getting the photo ready");
        (pdf ? Promise.resolve(file) : shrink(file)).then(function (blob) {
            status("");
            ui.send.disabled = busy;
            if (blob.size > MAX_FILE) { addError(pdf ? "That PDF is too big: send one under 2 MB." : "That photo is too big to send. Try a smaller one."); return; }
            const name = pdf ? file.name : (blob === file ? file.name : "photo.jpg");
            attachment = { blob: blob, name: name, kind: pdf ? "pdf" : "photo" };
            ui.chip.replaceChildren();
            ui.chip.appendChild(el("span", "", (pdf ? "PDF: " : "Photo: ") + file.name));
            ui.chip.appendChild(button("assistant-chip-remove", "Remove it", "close", clearAttachment));
            ui.chip.hidden = false;
            ui.input.placeholder = pdf ? "Say what to do with it, or just send" : "Say what it is, or just send";
            ui.input.focus();
        });
    }

    function clearAttachment() {
        attachment = null;
        if (!ui.chip) return;
        ui.chip.hidden = true;
        ui.chip.replaceChildren();
        ui.file.value = "";
        ui.input.placeholder = "Ask about your business";
    }

    function grow() {
        ui.input.style.height = "auto";
        ui.input.style.height = Math.min(ui.input.scrollHeight, 120) + "px";
    }

    function build() {
        panel = el("dialog", "assistant");
        panel.setAttribute("aria-label", "Assistant");
        panel.dataset.busy = "1";   // dialog.js closes a dialog when its dark backdrop is clicked; a chat panel has none, so a stray click must not close it
        const head = el("header", "assistant-head");
        head.appendChild(el("strong", "assistant-title", "Assistant"));
        const tools = el("span", "assistant-head-tools");
        if (Recognition) {
            ui.ear = button("assistant-ear", "Listen for the wake phrase", "ear", function () { if (ear) stopListening(false); else { unlock(); startListening(); } });
            ui.free = button("assistant-free", "Hands-free conversation", "free", function () { setHandsfree(!handsfree); });
            tools.appendChild(ui.ear);
            tools.appendChild(ui.free);
        }
        tools.appendChild(button("assistant-today", "What needs attention today", "today", function () { ask("What needs my attention today?", false); }));
        ui.more = button("assistant-more", "More", "more", function (e) { e.stopPropagation(); ui.menu.hidden = !ui.menu.hidden; });
        tools.appendChild(ui.more);
        tools.appendChild(button("assistant-shrink", "Make it small", "shrink", minimize));
        tools.appendChild(button("assistant-close", "Close the assistant", "close", closePanel));
        head.appendChild(tools);
        panel.appendChild(head);

        // The menu: reading typed answers aloud, the voice, what it remembers, a new chat
        ui.menu = el("div", "assistant-menu");
        ui.menu.hidden = true;
        function item(text, handler) {
            const b = el("button", "", text);
            b.type = "button";
            b.addEventListener("click", function () { ui.menu.hidden = true; handler(); });
            ui.menu.appendChild(b);
            return b;
        }
        ui.speakToggle = item("", function () {
            const on = keep("assistant.speak") !== "1";
            keep("assistant.speak", on ? "1" : "0");
            paintSpeak();
            if (on) unlock(); else stopSpeaking();
        });
        item("My voice", showVoices);
        item("What I remember", showMemory);
        item("Start a new chat", function () {
            if (recording) stopRecording(false);
            stopSpeaking();
            waiting = [];
            send(cfg.clear, new FormData()).then(function () { ui.body.replaceChildren(); welcome(); });
        });
        paintSpeak();
        panel.appendChild(ui.menu);
        panel.addEventListener("click", function (e) { if (!ui.menu.hidden && !ui.menu.contains(e.target)) ui.menu.hidden = true; });

        ui.body = el("div", "assistant-body");
        ui.body.setAttribute("role", "log");
        ui.body.setAttribute("aria-live", "polite");
        panel.appendChild(ui.body);
        ui.status = el("p", "assistant-status");
        ui.status.hidden = true;
        panel.appendChild(ui.status);
        ui.listenBar = el("div", "assistant-listening");
        ui.listenBar.setAttribute("role", "status");
        ui.listenBar.hidden = true;
        ui.listenBar.appendChild(wave());
        ui.listenText = el("span", "assistant-listening-text");
        ui.listenBar.appendChild(ui.listenText);
        panel.appendChild(ui.listenBar);

        // Not a <form>: dialog.js closes any dialog whose form is submitted, which is right for an edit overlay and wrong for a chat
        ui.chip = el("div", "assistant-chip");
        ui.chip.hidden = true;
        panel.appendChild(ui.chip);
        const form = el("div", "assistant-form");
        // No capture attribute: a phone then offers both the camera and its photos
        ui.file = el("input");
        ui.file.type = "file";
        ui.file.accept = "image/*,application/pdf";
        ui.file.hidden = true;
        ui.file.addEventListener("change", function () { pick(ui.file.files[0]); });
        ui.photo = button("assistant-photo", "Send a photo or a PDF", "camera", function () { if (!busy) ui.file.click(); });
        form.appendChild(ui.file);
        form.appendChild(ui.photo);
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

        panel.addEventListener("keydown", function (e) { if (e.key === "Escape") { if (ear) minimize(); else closePanel(); } });
        // On <html>, not <body>: nav.js swaps the whole body on every page change, which would wipe the chat (the progress bar does the same)
        document.documentElement.appendChild(panel);

        // The small panel: while listening, a tap outside the chat shrinks it to this, still listening, with the wave and where things stand
        ui.mini = el("div", "assistant-mini no-print");
        ui.mini.hidden = true;
        const grow_ = el("button", "assistant-mini-open");
        grow_.type = "button";
        grow_.setAttribute("aria-label", "Open the assistant");
        grow_.appendChild(wave());
        ui.miniText = el("span", "assistant-mini-text", "Assistant");
        grow_.appendChild(ui.miniText);
        grow_.addEventListener("click", expand);
        ui.mini.appendChild(grow_);
        ui.mini.appendChild(button("assistant-mini-close", "Stop listening and close", "close", closePanel));
        document.documentElement.appendChild(ui.mini);
    }

    function paintSpeak() {
        const on = keep("assistant.speak") === "1";
        ui.speakToggle.textContent = (on ? "✓ " : "") + "Read typed answers aloud";
        ui.speakToggle.setAttribute("aria-pressed", on ? "true" : "false");
    }

    function welcome() {
        addMessage("assistant", "Hello " + cfg.user + ". Ask me about sales, stock, customers, deliveries, what to restock or how to do something in the app. " +
            "I can prepare sales, pre-orders, purchases, plantings and more for you to confirm. Send a photo of a supplier's receipt or a tally of trays planted and I will fill it in for you. " +
            "Tell me how you like things and I will remember." +
            (Recognition ? " Tap the ear and just say “" + cfg.wake + "” and your question, or the headset for a hands-free conversation. The menu (…) lets you pick my voice." : ""),
            { plain: true });
    }

    function load() {
        status("Opening");
        fetch(cfg.state, { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) { return r.json(); }).then(function (s) {
            status("");
            ready = true;
            if (!s.enabled) { addError("The assistant is switched off for this business." + (s.can_enable ? " You can switch it on in Settings." : " Ask an admin to switch it on.")); return; }
            if (!s.configured) addError("The assistant is not connected to Gemini yet. An admin needs to add the Gemini key on the server.");
            cfg.wake = s.wake || cfg.wake;
            cfg.stop = s.stop || cfg.stop;
            cfg.voice = s.voice;
            voices = s.voices || [];
            if (!s.messages.length && !s.actions.length) welcome();
            s.messages.forEach(function (m) { addMessage(m.role, m.content, { plain: m.role === "assistant" && /^(Confirmed|Tried to confirm|Cancelled):/.test(m.content) }); });
            s.actions.forEach(addCard);
            scrollDown();
            paintEar();
            briefBadge(!!s.brief_due);
            if (s.brief_due) giveBrief();
        }).catch(function () { status(""); addError("Could not open the assistant. Check the connection and try again."); });
    }

    function openPanel() {
        if (!panel) build();
        if (panel.classList.contains("mini")) { expand(); return; }
        if (!panel.open) panel.show();
        fab.classList.add("open");
        fab.setAttribute("aria-expanded", "true");
        if (bubble) bubble.hidden = true;
        if (!ready) load();
        else if (fab.classList.contains("has-brief")) giveBrief();
        if (Recognition && keep("assistant.listen") === "1") {
            startListening();
            if (keep("assistant.handsfree") === "1") setHandsfree(true);
        }
        setTimeout(function () { ui.input.focus(); }, 50);
    }

    // Small, still listening (and still speaking an answer); tapping it brings the whole panel back
    function minimize() {
        if (!panel || !panel.open) return;
        ui.menu.hidden = true;
        panel.classList.add("mini");
        ui.mini.hidden = false;
        fab.hidden = true;
        paintEar();
    }

    function expand() {
        if (!panel) return;
        panel.classList.remove("mini");
        ui.mini.hidden = true;
        fab.hidden = false;
        setTimeout(function () { ui.input.focus(); scrollDown(); }, 50);
    }

    function closePanel() {
        if (recording) stopRecording(false);
        stopSpeaking();
        stopListening(true);
        if (panel) {
            panel.classList.remove("mini");
            ui.mini.hidden = true;
            if (panel.open) panel.close();
        }
        fab.hidden = false;
        fab.classList.remove("open");
        fab.setAttribute("aria-expanded", "false");
    }

    const fab = button("assistant-fab no-print", "Ask the assistant", "chat", function () {
        if (panel && panel.open && !panel.classList.contains("mini")) closePanel(); else openPanel();
    });
    fab.setAttribute("aria-expanded", "false");
    document.documentElement.appendChild(fab);

    // While it listens, a tap anywhere outside the chat shrinks it (it keeps listening); without listening the panel stays as it is
    document.addEventListener("pointerdown", function (e) {
        if (!panel || !panel.open || panel.classList.contains("mini") || !ear) return;
        if (panel.contains(e.target) || fab.contains(e.target) || ui.mini.contains(e.target) || (bubble && bubble.contains(e.target))) return;
        minimize();
    }, true);

    // The evening brief waiting: a dot on the button and a note beside it, until the panel is opened
    let bubble = null;
    if (cfg.briefWaiting === "1") {
        bubble = el("button", "assistant-bubble no-print", "Your evening brief is ready");
        bubble.type = "button";
        bubble.addEventListener("click", openPanel);
        document.documentElement.appendChild(bubble);
        fab.classList.add("has-brief");
    }

    // A page hidden (another tab, the phone locked) stops listening; it starts again when the page is back and the panel still open
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) { if (ear) stopListening(true); stopSpeaking(); }
        else if (panel && panel.open && Recognition && keep("assistant.listen") === "1") {
            startListening();
            if (keep("assistant.handsfree") === "1") setHandsfree(true);
        }
    });

    // An alert's link opens the panel (the evening brief's: /?assistant=brief)
    const asked = new URLSearchParams(location.search).get("assistant");
    if (asked) {
        history.replaceState(null, "", location.pathname + location.search.replace(/([?&])assistant=[^&]*&?/, "$1").replace(/[?&]$/, "") + location.hash);
        openPanel();
    }
})();
