// In-place navigation: clicking a link or saving a form fetches the next page in the background and swaps it in,
// instead of a full browser reload. The result is the exact same HTML the server would otherwise have sent for a
// real page load (so every page keeps working with no backend changes needed); only how it lands on screen
// changes - no white flash, no scroll jump back to the top of a page you were already on, no lost dropdown state
// in the header. Loaded once, like notify.js, and never re-run by itself.
//
// What is deliberately left to a real browser navigation: links to another site, "open in a new tab" clicks,
// download links (.csv/.pdf exports), and forms marked data-full-reload (the CSV/XLSX import uploads, kept as
// plain uploads for now). Anything that fails - offline, a server error, an unexpected response - falls back to
// a real navigation too, so the app is never left in a half-updated state.
(function () {
    const DOWNLOAD_PATH = /\.(csv|pdf)(?:$|\?)/i;

    function sameOrigin(url) {
        return url.origin === location.origin;
    }

    function isPlainClick(e) {
        return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
    }

    function runPageScripts() {
        const container = document.getElementById("page-scripts");
        if (!container) return;
        // Re-executes the current page's own scripts (inventory.js, item.js, ...) against the freshly swapped
        // content: setting innerHTML never runs embedded <script> tags, so each one is replaced with a live clone.
        Array.from(container.querySelectorAll("script")).forEach(function (old) {
            const fresh = document.createElement("script");
            Array.from(old.attributes).forEach(function (attr) { fresh.setAttribute(attr.name, attr.value); });
            fresh.textContent = old.textContent;
            old.replaceWith(fresh);
        });
    }

    function swap(html, finalUrl) {
        const doc = new DOMParser().parseFromString(html, "text/html");
        if (!doc.getElementById("page-main")) return false;   // not one of our own pages (e.g. a bare error page)
        document.title = doc.title;
        const theme = doc.documentElement.getAttribute("data-theme");
        if (theme) document.documentElement.setAttribute("data-theme", theme);
        const lookStyle = doc.documentElement.getAttribute("style");
        if (lookStyle) document.documentElement.setAttribute("style", lookStyle);
        document.body.innerHTML = doc.body.innerHTML;
        document.body.className = doc.body.className;
        runPageScripts();
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

    function navigate(url, init, pushState, scrollBefore) {
        // A saved form almost always posts to an action URL and is then redirected back to a display page (often
        // the very one it was submitted from) - so whether to keep the scroll position has to be judged against
        // where the fetch actually lands (finalUrl), not the address it started at.
        const startPage = location.href.split("#")[0];
        return fetch(url, Object.assign({ credentials: "same-origin" }, init))
            .then(function (response) { return response.text().then(function (html) { return { html: html, finalUrl: response.url }; }); })
            .then(function (result) {
                const kept = searchFocus();
                if (!swap(result.html, result.finalUrl)) throw new Error("not a page");
                if (pushState) history.pushState({ nav: true }, "", result.finalUrl);
                const samePage = result.finalUrl.split("#")[0] === startPage;
                window.scrollTo(0, samePage ? scrollBefore : 0);
                restoreSearchFocus(kept, result.finalUrl);
                // Lets dialog.js reopen an overlay whose save the server turned down
                document.dispatchEvent(new CustomEvent("nav:swapped", { detail: { url: result.finalUrl } }));
            });
    }

    document.addEventListener("click", function (e) {
        if (e.defaultPrevented || !isPlainClick(e)) return;
        const link = e.target.closest("a[href]");
        if (!link || link.closest("[data-full-reload]")) return;
        if (link.target && link.target !== "_self") return;
        if (link.hasAttribute("download")) return;
        const href = link.getAttribute("href");
        if (!href || /^(#|mailto:|tel:|javascript:)/i.test(href)) return;
        let url;
        try {
            url = new URL(href, location.href);
        } catch (err) {
            return;
        }
        if (!sameOrigin(url) || DOWNLOAD_PATH.test(url.pathname)) return;
        e.preventDefault();
        navigate(url.toString(), { method: "GET" }, true, window.scrollY).catch(function () {
            window.location.href = url.toString();
        });
    });

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
        const scrollBefore = window.scrollY;
        let init;
        if (method === "GET") {
            url.search = new URLSearchParams(new FormData(form)).toString();
            init = { method: "GET" };
        } else {
            init = { method: method, body: new FormData(form) };
        }
        navigate(url.toString(), init, true, scrollBefore).catch(function () {
            form.submit();   // a real submit, which this listener cannot intercept a second time
        });
    });

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

    window.addEventListener("popstate", function () {
        navigate(location.href, { method: "GET" }, false, 0).catch(function () {
            location.reload();
        });
    });
})();
