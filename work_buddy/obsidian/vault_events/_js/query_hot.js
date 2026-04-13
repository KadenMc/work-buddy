// Query hot files from the vault event ledger.
// __SINCE_DATE__ = "YYYY-MM-DD" (inclusive)
// __UNTIL_DATE__ = "YYYY-MM-DD" (inclusive)
// __LIMIT__ = max files to return
// __EXCLUDE_FOLDERS__ = JSON array of folder prefixes to exclude (e.g. '["journal","templates"]')
return (async () => {
    const ledger = window.__wb_vault_ledger;
    if (!ledger) return {error: "Vault event ledger not bootstrapped. Call bootstrap first."};

    const sinceDate = "__SINCE_DATE__";
    const untilDate = "__UNTIL_DATE__";
    const limit = __LIMIT__;
    const excludeFolders = __EXCLUDE_FOLDERS__;

    const now = Date.now();
    const results = [];

    for (const [path, stats] of Object.entries(ledger.files)) {
        // Apply folder exclusions — match anywhere in the path
        if (excludeFolders.some(f => path.startsWith(f + "/") || path.includes("/" + f + "/"))) continue;

        if (!stats.days) continue;

        // Sum modify counts within the date range
        let totalMods = 0;
        let activeDays = 0;
        for (const [day, count] of Object.entries(stats.days)) {
            if (day >= sinceDate && day <= untilDate) {
                totalMods += count;
                activeDays++;
            }
        }

        if (totalMods === 0 && activeDays === 0) continue;

        // Recency: days since last modification
        const daysSinceLast = Math.max(0, (now - stats.last) / 86400000);
        const recency = 1 / (1 + daysSinceLast);

        // Hot score: recency + frequency + intensity
        const hotScore = Math.round(
            (recency * 50 + activeDays * 10 + totalMods * 2) * 100
        ) / 100;

        results.push({
            path: path,
            hot_score: hotScore,
            total_modifications: totalMods,
            active_days: activeDays,
            last_modified: new Date(stats.last).toISOString(),
            created_in_window: stats.created
                ? new Date(stats.created).toISOString() : null,
        });
    }

    results.sort((a, b) => b.hot_score - a.hot_score);

    return {
        window: {since: sinceDate, until: untilDate},
        total_tracked: Object.keys(ledger.files).length,
        matching_files: results.length,
        files: results.slice(0, limit),
    };
})()
