// Execute a Datacore query and return serialized results.
// Placeholders: __QUERY__, __FIELDS__, __LIMIT__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const queryStr = `__QUERY__`;
    const requestedFields = '__FIELDS__';
    const limit = parseInt('__LIMIT__') || 50;

    // Parse fields list (comma-separated or empty for defaults)
    const fields = requestedFields ? requestedFields.split(',').map(f => f.trim()).filter(Boolean) : null;

    let results;
    try {
        results = api.query(queryStr);
    } catch(e) {
        return {error: 'Query failed: ' + e.message};
    }

    if (!Array.isArray(results)) {
        return {error: 'Query returned non-array: ' + typeof results};
    }

    const total = results.length;

    // Serialize each result into a flat dict
    const serialized = results.slice(0, limit).map(r => {
        if (typeof r.json === 'function') {
            // Page objects have a json() method — use it but strip deep nesting
            const j = r.json();
            // Flatten sections to summary only (avoid huge payloads)
            if (j.$sections) {
                j.$sections = j.$sections.map(s => ({
                    $title: s.$title,
                    $level: s.$level,
                    $ordinal: s.$ordinal,
                    $tags: s.$tags,
                    $blockCount: s.$blocks?.length ?? 0
                }));
            }
            // Convert Luxon timestamps to epoch ms
            if (typeof j.$ctime === 'object' && j.$ctime !== null) j.$ctime = r.value('$ctime')?.toString() ?? j.$ctime;
            if (typeof j.$mtime === 'object' && j.$mtime !== null) j.$mtime = r.value('$mtime')?.toString() ?? j.$mtime;

            if (fields) {
                const filtered = {};
                for (const f of fields) {
                    if (f in j) filtered[f] = j[f];
                    else {
                        // Try value() for computed fields
                        try { filtered[f] = r.value(f); } catch(_) { filtered[f] = undefined; }
                    }
                }
                return filtered;
            }
            return j;
        }

        // Non-page objects: manual serialization
        const flat = {};
        const keys = fields || Object.keys(r).filter(k => !k.startsWith('_')).slice(0, 20);
        for (const k of keys) {
            const v = r[k];
            if (v == null) flat[k] = null;
            else if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') flat[k] = v;
            else if (Array.isArray(v)) {
                flat[k] = v.length <= 10
                    ? v.map(item => typeof item === 'object' ? String(item) : item)
                    : v.slice(0, 10).map(item => typeof item === 'object' ? String(item) : item).concat(['... (' + v.length + ' total)']);
            }
            else if (typeof v === 'object') {
                try { flat[k] = JSON.parse(JSON.stringify(v)); } catch(_) { flat[k] = String(v); }
            }
            else flat[k] = String(v);
        }
        return flat;
    });

    return {total, returned: serialized.length, results: serialized};
})()
