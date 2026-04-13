// Summarize the vault's Datacore schema: object types, common fields, tags, path prefixes.
// Placeholder: __SAMPLE_LIMIT__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const sampleLimit = parseInt('__SAMPLE_LIMIT__') || 200;
    const types = ['@page', '@section', '@block', '@codeblock', '@list-item', '@task'];
    const summary = {object_types: {}, top_tags: [], path_prefixes: [], frontmatter_keys: []};

    // Count each object type
    for (const t of types) {
        try {
            const r = api.query(t);
            summary.object_types[t] = Array.isArray(r) ? r.length : 0;
        } catch(e) {
            summary.object_types[t] = {query_error: e.message};
        }
    }

    // Sample pages for tags and frontmatter keys
    try {
        const pages = api.query('@page');
        const tagCounts = {};
        const fmKeys = {};
        const prefixes = {};

        // Stride-sample across the full page set for representative coverage
        const stride = Math.max(1, Math.floor(pages.length / sampleLimit));
        const sample = [];
        for (let i = 0; i < pages.length && sample.length < sampleLimit; i += stride) {
            sample.push(pages[i]);
        }
        for (const p of sample) {
            // Tags
            const tags = p.$tags || [];
            for (const t of tags) {
                // Normalize to top-level tag
                const top = t.split('/').slice(0, 2).join('/');
                tagCounts[top] = (tagCounts[top] || 0) + 1;
            }

            // Frontmatter keys
            if (p.$frontmatter) {
                for (const k of Object.keys(p.$frontmatter)) {
                    fmKeys[k] = (fmKeys[k] || 0) + 1;
                }
            }

            // Path prefixes (first directory segment)
            const pathParts = p.$path?.split('/');
            if (pathParts && pathParts.length > 1) {
                const prefix = pathParts[0];
                prefixes[prefix] = (prefixes[prefix] || 0) + 1;
            }
        }

        // Sort and take top N
        summary.top_tags = Object.entries(tagCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 30)
            .map(([tag, count]) => ({tag, count}));

        summary.frontmatter_keys = Object.entries(fmKeys)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 30)
            .map(([key, count]) => ({key, count}));

        summary.path_prefixes = Object.entries(prefixes)
            .sort((a, b) => b[1] - a[1])
            .map(([prefix, count]) => ({prefix, count}));

        summary.pages_sampled = sample.length;
        summary.pages_total = pages.length;
    } catch(e) {
        summary.sample_error = e.message;
    }

    // Sample task statuses
    try {
        const tasks = api.query('@task');
        const statuses = {};
        for (const t of tasks.slice(0, sampleLimit)) {
            const s = t.$status || 'unknown';
            statuses[s] = (statuses[s] || 0) + 1;
        }
        summary.task_statuses = statuses;
        summary.tasks_total = tasks.length;
    } catch(_) {}

    return summary;
})()
