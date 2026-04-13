// Query recently modified files from the vault event ledger.
// __SINCE_MS__ = unix timestamp in ms for the start of window
// __LIMIT__ = max files to return
// __EXCLUDE_FOLDERS__ = JSON array of folder prefixes to exclude
return (async () => {
    const ledger = window.__wb_vault_ledger;
    if (!ledger) return {error: "Vault event ledger not bootstrapped."};

    const sinceMs = __SINCE_MS__;
    const limit = __LIMIT__;
    const excludeFolders = __EXCLUDE_FOLDERS__;

    const results = [];

    for (const [path, stats] of Object.entries(ledger.files)) {
        if (excludeFolders.some(f => path.startsWith(f + "/") || path.includes("/" + f + "/"))) continue;
        if (!stats.last || stats.last < sinceMs) continue;

        results.push({
            path: path,
            last_modified: new Date(stats.last).toISOString(),
            created_in_window: stats.created && stats.created >= sinceMs
                ? new Date(stats.created).toISOString() : null,
            renamed_from: stats.renamedFrom || null,
        });
    }

    results.sort((a, b) =>
        new Date(b.last_modified) - new Date(a.last_modified)
    );

    return {
        since: new Date(sinceMs).toISOString(),
        total_results: results.length,
        files: results.slice(0, limit),
    };
})()
