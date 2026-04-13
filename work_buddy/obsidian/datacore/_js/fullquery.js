// Execute a Datacore fullquery (includes timing and revision metadata).
// Placeholders: __QUERY__, __FIELDS__, __LIMIT__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const queryStr = `__QUERY__`;
    const requestedFields = '__FIELDS__';
    const limit = parseInt('__LIMIT__') || 50;

    const fields = requestedFields ? requestedFields.split(',').map(f => f.trim()).filter(Boolean) : null;

    let fq;
    try {
        fq = api.fullquery(queryStr);
    } catch(e) {
        return {error: 'Fullquery failed: ' + e.message};
    }

    const total = fq.results?.length ?? 0;

    // Serialize results (same logic as query.js)
    const serialized = (fq.results || []).slice(0, limit).map(r => {
        if (typeof r.json === 'function') {
            const j = r.json();
            if (j.$sections) {
                j.$sections = j.$sections.map(s => ({
                    $title: s.$title,
                    $level: s.$level,
                    $ordinal: s.$ordinal,
                    $tags: s.$tags,
                    $blockCount: s.$blocks?.length ?? 0
                }));
            }
            if (typeof j.$ctime === 'object' && j.$ctime !== null) j.$ctime = r.value('$ctime')?.toString() ?? j.$ctime;
            if (typeof j.$mtime === 'object' && j.$mtime !== null) j.$mtime = r.value('$mtime')?.toString() ?? j.$mtime;

            if (fields) {
                const filtered = {};
                for (const f of fields) {
                    if (f in j) filtered[f] = j[f];
                    else { try { filtered[f] = r.value(f); } catch(_) { filtered[f] = undefined; } }
                }
                return filtered;
            }
            return j;
        }

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

    return {
        total,
        returned: serialized.length,
        duration_s: fq.duration,
        revision: fq.revision,
        results: serialized
    };
})()
