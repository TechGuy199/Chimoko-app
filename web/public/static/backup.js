// Backups: the business's Excel books and every table as CSV, saved into a folder on this computer that the admin chose once.
// File names never change, so each backup replaces the files from the last one. At the end of the day (the reminder time set on
// the Backups page) the app asks whether to back up now; saying yes is what lets the browser write to the folder.
// Saving into a folder needs Chrome or Edge on a computer; any other browser downloads everything as one ZIP instead.
// Loaded once for admins, like notify.js; the Backups page uses it through window.ChimokoBackup.
(function () {
    const me = document.currentScript;
    if (!me) return;
    const supported = typeof window.showDirectoryPicker === "function";
    const FOLDER = "folder-" + me.dataset.biz;
    const SNOOZE = "backup-snooze-" + me.dataset.biz;
    const HOUR = 60 * 60 * 1000;
    let lastDone = me.dataset.last || "";
    let busy = false;

    // The latest settings: a page loaded since knows about a newer backup (another admin's) or a changed reminder time
    function settings() {
        const tag = document.querySelector("script[data-backup-files]") || me;
        if ((tag.dataset.last || "") > lastDone) lastDone = tag.dataset.last;
        return { hour: parseInt(tag.dataset.hour, 10), csrf: tag.dataset.csrf || me.dataset.csrf };
    }

    // The chosen folder is remembered in this browser (IndexedDB can keep a folder handle; localStorage cannot)
    function handles(mode, work) {
        return new Promise(function (resolve, reject) {
            const open = indexedDB.open("chimoko-backup", 1);
            open.onupgradeneeded = function () { open.result.createObjectStore("handles"); };
            open.onerror = function () { reject(open.error); };
            open.onsuccess = function () {
                const tx = open.result.transaction("handles", mode);
                const request = work(tx.objectStore("handles"));
                tx.oncomplete = function () { open.result.close(); resolve(request.result); };
                tx.onerror = function () { open.result.close(); reject(tx.error); };
            };
        });
    }
    function savedFolder() {
        return supported ? handles("readonly", function (s) { return s.get(FOLDER); }).catch(function () { return null; }) : Promise.resolve(null);
    }

    // The folder to save into, asking the browser for permission (or for a folder) when needed. Must run from a click.
    async function folder(chooseAnother) {
        let handle = chooseAnother ? null : await savedFolder();
        if (handle) {
            if (await handle.queryPermission({ mode: "readwrite" }) === "granted") return handle;
            if (await handle.requestPermission({ mode: "readwrite" }) === "granted") return handle;
            throw new Error("The browser was not allowed to save into the folder “" + handle.name + "”. Try again and choose Allow.");
        }
        handle = await window.showDirectoryPicker({ id: "chimoko-backup", mode: "readwrite", startIn: "documents" });
        await handles("readwrite", function (s) { return s.put(handle, FOLDER); });
        return handle;
    }

    async function writeFile(root, path, data) {
        const parts = path.split("/");
        let dir = root;
        for (let i = 0; i < parts.length - 1; i++) dir = await dir.getDirectoryHandle(parts[i], { create: true });
        const file = await dir.getFileHandle(parts[parts.length - 1], { create: true });
        const out = await file.createWritable();   // written to a temporary copy, which replaces the old file only once complete
        await out.write(data);
        await out.close();
    }

    async function fetchOk(url) {
        const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        // Signed out meanwhile: the answer is the login page, which must not be saved as a book
        if (!response.ok || (response.headers.get("Content-Type") || "").indexOf("text/html") === 0) {
            throw new Error("The backup could not be made. Refresh the page (you may need to sign in again) and try again.");
        }
        return response;
    }

    // One backup: every file into the folder, replacing last time's; then the server notes it, so the reminder knows today is done
    async function run(report) {
        report = report || function () {};
        if (!supported) {
            report("Downloading the backup as one ZIP file…");
            window.location.href = me.dataset.zip;   // the server notes the backup as it makes the ZIP
            lastDone = stampNow();
            return { zip: true };
        }
        const root = await folder(false);
        const files = await (await fetchOk(me.dataset.backupFiles)).json();
        for (let i = 0; i < files.length; i++) {
            report("Saving " + files[i].path + " (" + (i + 1) + " of " + files.length + ")…");
            await writeFile(root, files[i].path, await (await fetchOk(files[i].url)).blob());
        }
        await writeFile(root, "Backup info.txt", "Backed up " + new Date().toLocaleString() + "\r\n" + files.length + " files: the Excel books in Books, all of them in Master Backup.xlsx, and every table in Data (CSV).\r\n");
        const body = new URLSearchParams({ csrf: settings().csrf, files: String(files.length) });
        const done = await (await fetch(me.dataset.done, { method: "POST", body: body, credentials: "same-origin" })).json();
        lastDone = done.at || stampNow();
        return { files: files.length + 1, folder: root.name, at: lastDone };
    }

    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function today() {
        const d = new Date();
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    }
    function stampNow() { return today() + " " + new Date().toTimeString().slice(0, 8); }

    function snooze(ms) {
        try { localStorage.setItem(SNOOZE, String(Date.now() + ms)); } catch (e) { /* the reminder simply comes back */ }
    }
    function untilMidnight() {
        const midnight = new Date();
        midnight.setHours(24, 0, 0, 0);
        return midnight - Date.now();
    }

    // Due once the reminder time has passed and no backup has been made today, unless put off
    function due() {
        const s = settings();
        if (!(s.hour >= 0) || new Date().getHours() < s.hour || lastDone.slice(0, 10) >= today()) return false;
        let until = 0;
        try { until = parseInt(localStorage.getItem(SNOOZE) || "0", 10); } catch (e) { until = 0; }
        return Date.now() >= until;
    }

    function failure(error) {
        if (error && error.name === "AbortError") return "No folder was chosen, so nothing was saved.";
        return (error && error.message) || "The backup could not be saved.";
    }

    // The end-of-day question. It is built here, on whatever page is open, and closes itself afterwards.
    async function remind() {
        if (busy || document.getElementById("backup-prompt") || document.querySelector("dialog[open]") || !due()) return;
        const where = supported ? await savedFolder() : null;
        const dialog = document.createElement("dialog");
        dialog.id = "backup-prompt";
        dialog.className = "modal no-print";
        dialog.innerHTML =
            '<div class="modal-body"><h2>End of day backup</h2>' +
            "<p>Save today’s backup to this computer? The Sales, Purchase, Planting and Inventory books, a master workbook with every book, and all the records as CSV files.</p>" +
            '<p class="muted" data-where></p><p class="muted" data-status role="status"></p>' +
            '<button type="button" class="btn success wide" data-go>Back up now</button>' +
            '<div class="toolbar spaced"><button type="button" class="btn" data-later>Remind me in an hour</button>' +
            '<button type="button" class="btn ghost" data-skip>Not today</button></div></div>';
        dialog.querySelector("[data-where]").textContent = !supported
            ? "This browser downloads the backup as one ZIP file. To have each backup replace the last one in a folder, use Chrome or Edge."
            : where ? "It replaces the last backup in the folder “" + where.name + "”."
            : "The first time, choose the folder to keep backups in; later backups replace the files there.";
        if (lastDone) dialog.querySelector("[data-where]").textContent += " Last backup: " + lastDone.slice(0, 16) + ".";
        const status = dialog.querySelector("[data-status]");
        const buttons = Array.from(dialog.querySelectorAll("button"));
        let answered = false;

        dialog.querySelector("[data-go]").addEventListener("click", async function () {
            answered = busy = true;
            dialog.dataset.busy = "1";
            buttons.forEach(function (b) { b.disabled = true; });
            try {
                const result = await run(function (text) { status.textContent = text; });
                status.textContent = result.zip ? "The ZIP file is downloading." : "Saved " + result.files + " files to “" + result.folder + "”.";
                setTimeout(function () { dialog.close(); }, 2500);
            } catch (error) {
                status.textContent = failure(error);
                buttons.forEach(function (b) { b.disabled = false; });
                answered = false;
            } finally {
                busy = false;
                delete dialog.dataset.busy;
            }
        });
        dialog.querySelector("[data-later]").addEventListener("click", function () { answered = true; snooze(HOUR); dialog.close(); });
        dialog.querySelector("[data-skip]").addEventListener("click", function () { answered = true; snooze(untilMidnight()); dialog.close(); });
        dialog.addEventListener("cancel", function (e) { if (busy) e.preventDefault(); });
        // Closed any other way (Esc, a click outside): asked again in an hour
        dialog.addEventListener("close", function () { if (!answered) snooze(HOUR); dialog.remove(); });
        document.body.appendChild(dialog);
        dialog.showModal();
    }

    window.ChimokoBackup = {
        supported: supported,
        run: run,
        chooseFolder: function () { return folder(true); },
        folderName: function () { return savedFolder().then(function (h) { return h ? h.name : ""; }); },
        lastDone: function () { return lastDone; },
        failure: failure,
    };

    setTimeout(remind, 9000);   // after the launch screen
    setInterval(remind, 5 * 60 * 1000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) remind(); });
})();
