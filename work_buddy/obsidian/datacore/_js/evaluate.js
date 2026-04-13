// Evaluate a Datacore expression.
// Placeholders: __EXPRESSION__, __SOURCE_PATH__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const expression = `__EXPRESSION__`;
    const sourcePath = '__SOURCE_PATH__' || undefined;

    try {
        const result = api.evaluate(expression, sourcePath ? {this: api.page(sourcePath)} : undefined);
        // Attempt to serialize
        if (result === null || result === undefined) return {result: null};
        if (typeof result === 'string' || typeof result === 'number' || typeof result === 'boolean') return {result};
        if (Array.isArray(result)) return {result: result.slice(0, 100).map(r => typeof r === 'object' ? String(r) : r)};
        try { return {result: JSON.parse(JSON.stringify(result))}; } catch(_) { return {result: String(result)}; }
    } catch(e) {
        return {error: 'Evaluate failed: ' + e.message};
    }
})()
