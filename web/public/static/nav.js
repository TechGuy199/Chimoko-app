// In-place navigation: clicking a link or saving a form fetches the next page in the background and swaps it in,
// instead of a full browser reload. The result is the exact same HTML the server would otherwise have sent for a
// real page load (so every page keeps working with no backend changes needed); only how it lands on screen
// changes - no white flash, no scroll jump back to the top of a page you were already on, no lost dropdown state
// in the header. Loaded once, like notify.js, and never re-run by itself.
//
// So that it feels quick even when the server is far away:
// - a thin bar runs along the top while a page is on its way (only if it takes more than a moment);
// - a link starts loading when the pointer rests on it (or a finger touches it), so part of the wait is over by the
//   time it is clicked. Only links that just show a page are fetched early, the copy is used once and only for a few
//   seconds, and any save throws all early copies away, so a page is never shown with data older than a save;
// - Back and Forward show the page as it was at once, from memory, then bring it up to date without moving it;
// - the latest click wins: a page that arrives after another was asked for is dropped, never swapped in over it;
// - a form being saved cannot be sent a second time by a double click;
// - Back and Forward return to where the page was scrolled to.
//
// What is deliberately left to a real browser navigation: links to another site, "open in a new tab" clicks,
// download links (.csv/.pdf/.xlsx/.zip exports), forms marked data-full-reload (the CSV/XLSX import uploads, kept as
// plain uploads for now), and signing in or out (the signed-in page loads its own scripts: alerts, backups). Anything
// that fails - offline, a server error, an unexpected response - falls back to a real navigation too, so the app is
// never left in a half-updated state. With no connection (or on a page shown from the device's offline copy, see sw.js) a save is not
// tried at all: the page says so, and points to the offline screens.
(function () {
    const DOWNLOAD_PATH = /\.(csv|pdf|xlsx|zip)(?:$|\?)/i;
    // Links that do something when followed (opening an alert marks it read), so they are never fetched early
    const NOT_EARLY = /\/notifications\/\d+\/open$/;
    const EARLY_FOR = 8000;       // ms an early copy stays usable
    const HOVER_WAIT = 40;        // ms the pointer rests on a link before it is fetched early
    const BAR_AFTER = 60;         // ms before the progress bar appears
    const REMEMBERED = 15;        // pages kept in memory for Back and Forward

    function sameOrigin(url) {
        return url.origin === location.origin;
    }

    function isPlainClick(e) {
        return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
    }

    // --- The progress bar: on <html>, outside <body>, so swapping the page never removes it ---
    let bar = null;
    let barTimer = null;
    function progressStart() {
        clearTimeout(barTimer);
        document.documentElement.classList.add("nav-busy");
        barTimer = setTimeout(function () {
            if (!bar) {
                bar = document.createElement("div");
                bar.id = "nav-progress";
                bar.setAttribute("aria-hidden", "true");
                document.documentElement.appendChild(bar);
            }
            bar.className = "";
            void bar.offsetWidth;   // restart the animation from the left
            bar.className = "running";
        }, BAR_AFTER);
    }
    function progressDone() {
        clearTimeout(barTimer);
        document.documentElement.classList.remove("nav-busy");
        if (bar && bar.className === "running") bar.className = "finishing";
    }

    function runScripts(container) {
        if (!container) return;
        // Setting innerHTML never runs embedded <script> tags, so each one is replaced with a live clone
        Array.from(container.querySelectorAll("script")).forEach(function (old) {
            const fresh = document.createElement("script");
            Array.from(old.attributes).forEach(function (attr) { fresh.setAttribute(attr.name, attr.value); });
            fresh.textContent = old.textContent;
            old.replaceWith(fresh);
        });
    }

    // Returns true when swapped, false when the response is not one of our pages, and "reload" when the next page needs a
    // real page load: signing in or out changes which scripts the page runs once (alerts, backups), so those must load afresh.
    function swap(html) {
        const doc = new DOMParser().parseFromString(html, "text/html");
        if (!doc.getElementById("page-main")) return false;   // not one of our own pages (e.g. a bare error page)
        if (!!doc.getElementById("app-header") !== !!document.getElementById("app-header")) return "reload";
        document.title = doc.title;
        ["data-theme", "data-accent", "data-size", "data-font", "data-spacing"].forEach(function (name) {
            const value = doc.documentElement.getAttribute(name);
            if (value !== null) document.documentElement.setAttribute(name, value);
        });
        const lookStyle = doc.documentElement.getAttribute("style");
        if (lookStyle) document.documentElement.setAttribute("style", lookStyle);
        // The navigation bar keeps its place: it is drawn again with the page, so where it was scrolled to is put back
        const sideNav = document.querySelector(".side-nav");
        const sideAt = sideNav ? sideNav.scrollTop : 0;
        document.body.innerHTML = doc.body.innerHTML;
        document.body.className = doc.body.className;
        const newSide = document.querySelector(".side-nav");
        if (newSide && sideAt) newSide.scrollTop = sideAt;
        // The page's own scripts: those in its content, and the ones it lists in #page-scripts. The app-wide ones outside
        // these two are loaded once and keep running.
        runScripts(document.getElementById("page-main"));
        runScripts(document.getElementById("page-scripts"));
        return true;
    }

    // Search as you type: the results arrive while the box still has focus, often with a letter or two typed since the
    // search was sent. The box in the new page is given back the focus and exactly what is in it now, so typing is never
    // interrupted. Only for search boxes (type="search" with an id) and only when the page stays the same.
    function searchFocus() {
        const el = document.activeElement;
        if (!el || el.tagName !== "INPUT" || el.type !== "search" || !el.id) return null;
        return { id: el.id, value: el.value, path: location.pathname };
    }

    function restoreSearchFocus(kept, finalUrl) {
        if (!kept || new URL(finalUrl).pathname !== kept.path) return;
        const el = document.getElementById(kept.id);
        if (!el || el.type !== "search") return;
        el.value = kept.value;
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
    }

    function load(url, init, signal) {
        // A page asked for (not a save) says so, so the service worker keeps a copy of it for offline viewing
        if ((init.method || "GET").toUpperCase() === "GET") init = Object.assign({}, init, { headers: Object.assign({ "X-Nav-Page": "1" }, init.headers) });
        return fetch(url, Object.assign({ credentials: "same-origin", signal: signal }, init))
            .then(function (response) {
                // A save comes back as the page to show next, with its address in X-Nav-Url (see FollowSaves in app.py)
                const shown = response.headers.get("X-Nav-Url");
                const finalUrl = shown ? new URL(shown, location.href).toString() : response.url;
                return response.text().then(function (html) { return { html: html, finalUrl: finalUrl }; });
            });
    }

    // --- Pages fetched early (pointer resting on a link) ---
    const early = new Map();   // url -> { at, settled, promise }
    function earlyCopy(url) {
        const entry = early.get(url);
        early.delete(url);   // used once at most
        return entry && Date.now() - entry.at < EARLY_FOR ? entry.promise : null;
    }
    function fetchEarly(url) {
        const now = Date.now();
        if (early.has(url) && now - early.get(url).at < EARLY_FOR) return;
        let inFlight = 0;
        early.forEach(function (entry, key) {
            if (now - entry.at >= EARLY_FOR) early.delete(key);
            else if (!entry.settled) inFlight += 1;
        });
        if (inFlight >= 2) return;
        const entry = { at: now, settled: false };
        entry.promise = load(url, { method: "GET" }).then(function (result) {
            entry.settled = true;
            return result;
        }, function (err) {
            entry.settled = true;
            if (early.get(url) === entry) early.delete(url);
            throw err;
        });
        entry.promise.catch(function () { /* used only if the link is clicked, and then the click fetches it again */ });
        early.set(url, entry);
    }

    // --- Pages seen, for Back and Forward ---
    const seen = new Map();   // address (without #) -> the page's HTML, newest last
    function remember(url, html) {
        const key = url.split("#")[0];
        seen.delete(key);
        seen.set(key, html);
        while (seen.size > REMEMBERED) seen.delete(seen.keys().next().value);
    }

    // --- Navigating ---
    let latest = 0;            // the newest navigation; an older one that finishes later is dropped
    let pageFetch = null;      // the AbortController of a page being fetched (GET), so a newer click can stop it

    function navigate(url, init, pushState, scrollBefore, filtering, scrollTo) {
        // A saved form almost always posts to an action URL and is then redirected back to a display page (often
        // the very one it was submitted from) - so whether to keep the scroll position has to be judged against
        // where the fetch actually lands (finalUrl), not the address it started at. A search or filter form (filtering)
        // only changes what the same page lists, so the page counts as the same whatever its query string.
        const mine = ++latest;
        const startPage = location.href.split("#")[0];
        const startPath = location.pathname;
        const hash = new URL(url, location.href).hash;   // fetch drops it, so it is put back on the address and scrolled to
        const isGet = (init.method || "GET").toUpperCase() === "GET";
        if (pageFetch) pageFetch.abort();
        pageFetch = null;
        if (!isGet) early.clear();   // a save changes data, so nothing fetched before it may be shown after it
        let request = isGet ? earlyCopy(url.split("#")[0]) : null;
        if (!request) {
            const controller = isGet && window.AbortController ? new AbortController() : null;
            if (controller) pageFetch = controller;
            request = load(url, init, controller ? controller.signal : undefined);
        }
        progressStart();
        return request
            .then(function (result) {
                if (mine !== latest) return;   // another page was asked for meanwhile; that one wins
                const kept = searchFocus();
                const samePage = filtering ? new URL(result.finalUrl).pathname === startPath : result.finalUrl.split("#")[0] === startPage;
                // The same page brought up to date stays still; a new page makes its entrance (styles.css)
                document.documentElement.classList.toggle("nav-quiet", samePage);
                const swapped = swap(result.html);
                if (swapped === true && isGet) remember(result.finalUrl, result.html);
                if (swapped === "reload") {
                    window.location.href = result.finalUrl;
                    return;
                }
                if (!swapped) throw new Error("not a page");
                if (pushState) history.pushState({ nav: true }, "", result.finalUrl + (new URL(result.finalUrl).hash ? "" : hash));
                const target = hash && !samePage ? document.getElementById(decodeURIComponent(hash.slice(1))) : null;
                // Instant, not the page's smooth scrolling: the new page should simply be there, where it belongs
                if (target) target.scrollIntoView({ behavior: "instant" });
                else window.scrollTo({ top: typeof scrollTo === "number" ? scrollTo : samePage ? scrollBefore : 0, left: 0, behavior: "instant" });
                restoreSearchFocus(kept, result.finalUrl);
                // Lets dialog.js reopen an overlay whose save the server turned down, and notify.js redraw the alerts
                document.dispatchEvent(new CustomEvent("nav:swapped", { detail: { url: result.finalUrl } }));
            })
            .then(function () {
                if (mine === latest) progressDone();
            }, function (err) {
                if (mine === latest) progressDone();
                if (err && err.name === "AbortError") return;   // a newer click stopped this one on purpose
                if (mine !== latest) return;
                throw err;
            });
    }

    // Where the page was scrolled to is kept with its place in the history, for Back and Forward
    function rememberScroll() {
        try {
            history.replaceState(Object.assign({}, history.state, { nav: true, scroll: window.scrollY }), "");
        } catch (err) { /* some browsers limit how often this may be called; only the scroll position is lost */ }
    }

    function followable(link) {
        if (!link || link.closest("[data-full-reload]")) return null;
        if (link.target && link.target !== "_self") return null;
        if (link.hasAttribute("download")) return null;
        const href = link.getAttribute("href");
        if (!href || /^(#|mailto:|tel:|javascript:)/i.test(href)) return null;
        let url;
        try {
            url = new URL(href, location.href);
        } catch (err) {
            return null;
        }
        if (!sameOrigin(url) || DOWNLOAD_PATH.test(url.pathname)) return null;
        return url;
    }

    document.addEventListener("click", function (e) {
        if (e.defaultPrevented || !isPlainClick(e)) return;
        const url = followable(e.target.closest("a[href]"));
        if (!url) return;
        e.preventDefault();
        rememberScroll();
        navigate(url.toString(), { method: "GET" }, true, window.scrollY).catch(function () {
            window.location.href = url.toString();
        });
    });

    // Early fetching: a pointer resting on a link, or a finger touching it
    let hoverTimer = null;
    function earlyTarget(e) {
        const link = e.target.closest ? e.target.closest("a[href]") : null;
        const url = followable(link);
        if (!url || link.hasAttribute("data-no-prefetch") || NOT_EARLY.test(url.pathname)) return null;
        const bare = url.toString().split("#")[0];
        return bare === location.href.split("#")[0] ? null : bare;
    }
    document.addEventListener("mouseover", function (e) {
        const url = earlyTarget(e);
        if (!url) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(function () { fetchEarly(url); }, HOVER_WAIT);
    });
    document.addEventListener("mouseout", function (e) {
        if (e.target.closest && e.target.closest("a[href]")) clearTimeout(hoverTimer);
    });
    document.addEventListener("touchstart", function (e) {
        const url = earlyTarget(e);
        if (url) fetchEarly(url);
    }, { passive: true });

    document.addEventListener("submit", function (e) {
        if (e.defaultPrevented) return;   // a data-confirm dialog the user declined already cancelled this
        const form = e.target;
        if (!(form instanceof HTMLFormElement) || form.closest("[data-full-reload]")) return;
        if (form.target && form.target !== "_self") return;
        let url;
        try {
            url = new URL(form.getAttribute("action") || location.href, location.href);
        } catch (err) {
            return;
        }
        if (!sameOrigin(url)) return;
        e.preventDefault();
        const method = (form.getAttribute("method") || "GET").toUpperCase();
        // No connection, or a page shown from the offline copy: nothing is sent, and the page says why
        if (method !== "GET" && (!navigator.onLine || document.getElementById("offline-copy"))) {
            e.preventDefault();
            offlineNote();
            return;
        }
        // A save already on its way is not sent again by a second click
        if (method !== "GET" && form.dataset.sending) return;
        const scrollBefore = window.scrollY;
        const data = formData(form, e.submitter);
        let init;
        if (method === "GET") {
            // Empty fields are left out of the address (the server reads a missing field and an empty one alike)
            const query = new URLSearchParams();
            data.forEach(function (value, key) { if (value !== "") query.append(key, value); });
            url.search = query.toString();
            init = { method: "GET" };
        } else {
            // The server sends the next page straight back instead of a redirect to it: one round trip, not two
            init = { method: method, body: data, headers: { "X-Nav": "follow" } };
            form.dataset.sending = "1";
            form.classList.add("is-sending");
        }
        function ready() {
            delete form.dataset.sending;
            form.classList.remove("is-sending");
        }
        rememberScroll();
        navigate(url.toString(), init, true, scrollBefore, method === "GET").then(ready, function (err) {
            ready();
            // The server could not be reached (no internet, even with Wi-Fi on): said here, rather than the browser's own error page
            if (method !== "GET" && err instanceof TypeError) { offlineNote(); return; }
            if (document.body.contains(form)) form.submit();   // a real submit, which this listener cannot intercept a second time
            else window.location.href = url.toString();
        });
    });

    function offlineNote() {
        const main = document.getElementById("page-main");
        if (!main) return;
        let note = document.getElementById("offline-note");
        if (!note) {
            note = document.createElement("div");
            note.id = "offline-note";
            note.className = "flash error no-print";
            note.appendChild(document.createTextNode("No connection: nothing was saved. Sales can still be made under "));
            const link = document.createElement("a");
            link.href = "/static/offline.html";
            link.setAttribute("data-full-reload", "");
            link.textContent = "Work offline";
            note.appendChild(link);
            note.appendChild(document.createTextNode("; everything else waits for the connection."));
            main.insertBefore(note, main.firstChild);
        }
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    // The form's fields, and the button that was pressed (its name and value), as a real submit would send them
    function formData(form, submitter) {
        try {
            return new FormData(form, submitter || undefined);
        } catch (err) {
            const data = new FormData(form);   // an older browser: the pressed button is added by hand
            if (submitter && submitter.name) data.append(submitter.name, submitter.value);
            return data;
        }
    }

    // Dropdown menus (<details class="menu">) stay on screen: a panel that would open past either edge of a narrow
    // screen (the Actions menu on Reports, when its button wraps to the left) is nudged back in. The header's own menus
    // already open full width on a phone (styles.css). "toggle" does not bubble, hence the capture phase.
    document.addEventListener("toggle", function (e) {
        const menu = e.target;
        if (!(menu instanceof HTMLDetailsElement) || !menu.open || !menu.classList.contains("menu")) return;
        const panel = menu.querySelector(".menu-panel");
        if (!panel) return;
        panel.style.marginRight = "";
        // The panel grows in from its top right corner, so its right edge is where it will settle; its width is
        // read without the opening animation's scale
        const edge = 8;
        const right = panel.getBoundingClientRect().right;
        const left = right - panel.offsetWidth;
        const screen = document.documentElement.clientWidth;
        if (left < edge) panel.style.marginRight = (left - edge) + "px";
        else if (right > screen - edge) panel.style.marginRight = (right - (screen - edge)) + "px";
    }, true);

    if ("scrollRestoration" in history) history.scrollRestoration = "manual";
    window.addEventListener("popstate", function (e) {
        const scroll = e.state && typeof e.state.scroll === "number" ? e.state.scroll : 0;
        const here = location.href;
        const kept = seen.get(here.split("#")[0]);
        if (kept) {
            // At once, as it was; then the live page replaces it where it stands, unless the person has started typing in it
            const mine = ++latest;
            document.documentElement.classList.add("nav-quiet");
            if (swap(kept) === true) {
                window.scrollTo({ top: scroll, left: 0, behavior: "instant" });
                document.dispatchEvent(new CustomEvent("nav:swapped", { detail: { url: here } }));
                load(here, { method: "GET" }).then(function (result) {
                    if (mine !== latest || location.href !== here) return;
                    const active = document.activeElement;
                    if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
                    const y = window.scrollY;
                    if (swap(result.html) === true) {
                        remember(here, result.html);
                        window.scrollTo({ top: y, left: 0, behavior: "instant" });
                        document.dispatchEvent(new CustomEvent("nav:swapped", { detail: { url: here } }));
                    }
                }).catch(function () { /* the copy stays; offline, the service worker answers with its own */ });
                return;
            }
        }
        navigate(location.href, { method: "GET" }, false, 0, false, scroll).catch(function () {
            location.reload();
        });
    });
})();
