// Bootstrap vault event tracking. Idempotent — safe to call multiple times.
// __WINDOW_DAYS__ = rolling window in days (default 7)
// Stores compact per-file stats in localStorage, never raw event streams.
return (async () => {
    const WINDOW_DAYS = __WINDOW_DAYS__;
    const STORAGE_KEY = "wb-vault-ledger";
    const FLAG = "__wb_vault_ledger_active";

    // Already bootstrapped this session?
    if (window[FLAG]) {
        return {
            status: "already_active",
            file_count: Object.keys(window.__wb_vault_ledger.files).length,
            since: window.__wb_vault_ledger.bootstrapped,
        };
    }

    // Load persisted state or initialize
    let ledger;
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        ledger = raw ? JSON.parse(raw) : null;
    } catch (e) {
        ledger = null;
    }

    if (!ledger || !ledger.files) {
        ledger = { files: {}, windowDays: WINDOW_DAYS };
    }
    ledger.windowDays = WINDOW_DAYS;
    ledger.bootstrapped = new Date().toISOString();

    // Compact: remove days outside the window
    const cutoff = new Date(Date.now() - WINDOW_DAYS * 86400000)
        .toISOString().slice(0, 10);
    for (const [path, stats] of Object.entries(ledger.files)) {
        if (!stats.days) continue;
        for (const d of Object.keys(stats.days)) {
            if (d < cutoff) delete stats.days[d];
        }
        // Remove file entry if no days remain and not recently active
        if (Object.keys(stats.days).length === 0) {
            delete ledger.files[path];
        }
    }

    // Reconcile: scan vault for files modified while plugin was offline
    const now = Date.now();
    const cutoffMs = now - WINDOW_DAYS * 86400000;
    const today = new Date().toISOString().slice(0, 10);
    let reconciled = 0;

    for (const file of app.vault.getMarkdownFiles()) {
        const mtime = file.stat.mtime;
        if (mtime < cutoffMs) continue;

        const fp = file.path;
        const isNew = !ledger.files[fp];
        if (isNew) {
            ledger.files[fp] = { last: 0, days: {}, created: null };
        }
        const entry = ledger.files[fp];

        // Update last-modified if file is newer than what we tracked
        if (mtime > (entry.last || 0)) {
            entry.last = mtime;
            reconciled++;
        }
        // Ensure the modification day exists in the daily counts
        const dateKey = new Date(mtime).toISOString().slice(0, 10);
        if (!entry.days) entry.days = {};
        if (!entry.days[dateKey]) {
            entry.days[dateKey] = 1;
        }
    }

    // Save to memory and persist
    window.__wb_vault_ledger = ledger;
    window[FLAG] = true;

    // Debounced save function
    let saveTimeout = null;
    window.__wb_vault_ledger_save = () => {
        if (saveTimeout) clearTimeout(saveTimeout);
        saveTimeout = setTimeout(() => {
            try {
                localStorage.setItem(STORAGE_KEY,
                    JSON.stringify(window.__wb_vault_ledger));
            } catch (e) { /* storage full — degrade silently */ }
        }, 5000);  // 5s debounce
    };

    // Register event listeners (after layout ready to avoid startup create spam)
    const handler = (type, file, oldPath) => {
        if (!file?.path) return;
        const fp = file.path;
        const ts = Date.now();
        const dateKey = new Date(ts).toISOString().slice(0, 10);
        const ledger = window.__wb_vault_ledger;
        if (!ledger) return;

        if (type === "delete") {
            delete ledger.files[fp];
        } else if (type === "rename") {
            // Move stats from old path to new path
            if (oldPath && ledger.files[oldPath]) {
                ledger.files[fp] = ledger.files[oldPath];
                delete ledger.files[oldPath];
            }
            if (!ledger.files[fp]) {
                ledger.files[fp] = { last: ts, days: {}, created: null };
            }
            ledger.files[fp].last = ts;
            ledger.files[fp].renamedFrom = oldPath || null;
        } else {
            // create or modify
            if (!ledger.files[fp]) {
                ledger.files[fp] = { last: ts, days: {}, created: null };
            }
            const entry = ledger.files[fp];
            entry.last = ts;
            if (!entry.days) entry.days = {};
            entry.days[dateKey] = (entry.days[dateKey] || 0) + 1;
            if (type === "create") {
                entry.created = ts;
            }
        }

        window.__wb_vault_ledger_save();
    };

    // Use workspace.onLayoutReady to avoid startup create-event flood
    if (app.workspace.layoutReady) {
        app.vault.on("create", (f) => handler("create", f));
        app.vault.on("modify", (f) => handler("modify", f));
        app.vault.on("rename", (f, old) => handler("rename", f, old));
        app.vault.on("delete", (f) => handler("delete", f));
    } else {
        app.workspace.onLayoutReady(() => {
            app.vault.on("create", (f) => handler("create", f));
            app.vault.on("modify", (f) => handler("modify", f));
            app.vault.on("rename", (f, old) => handler("rename", f, old));
            app.vault.on("delete", (f) => handler("delete", f));
        });
    }

    // Initial save
    window.__wb_vault_ledger_save();

    return {
        status: "bootstrapped",
        file_count: Object.keys(ledger.files).length,
        reconciled: reconciled,
        window_days: WINDOW_DAYS,
        since: ledger.bootstrapped,
    };
})()
