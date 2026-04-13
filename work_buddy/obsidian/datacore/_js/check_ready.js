// Check if the Datacore plugin is installed, initialized, and queryable.
return (async () => {
    const plugin = app.plugins.plugins['datacore'];
    if (!plugin) return {ready: false, reason: 'Datacore plugin not found'};

    const api = plugin.api || window.datacore;
    if (!api) return {ready: false, reason: 'Datacore API not available'};

    const version = plugin.manifest?.version || null;
    const initialized = api.initialized !== false;
    const revision = api.revision ?? null;

    // Sample counts to verify index is populated
    let counts = {};
    try {
        const types = ['@page', '@section', '@task'];
        for (const t of types) {
            const r = api.query(t);
            counts[t] = Array.isArray(r) ? r.length : 0;
        }
    } catch(e) {
        return {ready: false, reason: 'Query failed: ' + e.message, version, initialized, revision};
    }

    const ready = initialized && counts['@page'] > 0;
    return {ready, version, initialized, revision, counts};
})()
