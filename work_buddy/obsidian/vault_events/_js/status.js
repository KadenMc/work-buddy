// Get vault event ledger status and stats — never dumps the full ledger.
return (async () => {
    const ledger = window.__wb_vault_ledger;
    const active = !!window.__wb_vault_ledger_active;

    if (!ledger) {
        return {
            active: false,
            reason: "Ledger not bootstrapped",
            storage_exists: !!localStorage.getItem("wb-vault-ledger"),
        };
    }

    const fileCount = Object.keys(ledger.files).length;

    // Count total modifications across all files
    let totalMods = 0;
    let oldestDay = "9999-99-99";
    let newestDay = "0000-00-00";
    for (const stats of Object.values(ledger.files)) {
        for (const [day, count] of Object.entries(stats.days || {})) {
            totalMods += count;
            if (day < oldestDay) oldestDay = day;
            if (day > newestDay) newestDay = day;
        }
    }

    // Storage size
    let storageBytes = 0;
    try {
        const raw = localStorage.getItem("wb-vault-ledger");
        storageBytes = raw ? new Blob([raw]).size : 0;
    } catch (e) {}

    return {
        active: active,
        file_count: fileCount,
        total_modifications: totalMods,
        window_days: ledger.windowDays,
        date_range: fileCount > 0
            ? {oldest: oldestDay, newest: newestDay} : null,
        bootstrapped: ledger.bootstrapped,
        storage_bytes: storageBytes,
    };
})()
