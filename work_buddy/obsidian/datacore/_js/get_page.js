// Get a single page by vault-relative path.
// Placeholders: __PATH__, __FIELDS__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const filePath = '__PATH__';
    const requestedFields = '__FIELDS__';
    const fields = requestedFields ? requestedFields.split(',').map(f => f.trim()).filter(Boolean) : null;

    const page = api.page(filePath);
    if (!page) return {error: 'Page not found: ' + filePath};

    let j;
    try {
        j = page.json();
    } catch(e) {
        return {error: 'Serialization failed: ' + e.message};
    }

    // Flatten sections to summaries
    if (j.$sections) {
        j.$sections = j.$sections.map(s => ({
            $title: s.$title,
            $level: s.$level,
            $ordinal: s.$ordinal,
            $tags: s.$tags,
            $blockCount: s.$blocks?.length ?? 0
        }));
    }

    // Convert timestamps
    if (typeof j.$ctime === 'object' && j.$ctime !== null) j.$ctime = page.value('$ctime')?.toString() ?? j.$ctime;
    if (typeof j.$mtime === 'object' && j.$mtime !== null) j.$mtime = page.value('$mtime')?.toString() ?? j.$mtime;

    if (fields) {
        const filtered = {};
        for (const f of fields) {
            if (f in j) filtered[f] = j[f];
            else { try { filtered[f] = page.value(f); } catch(_) { filtered[f] = undefined; } }
        }
        return filtered;
    }
    return j;
})()
