// vault_recon: single-walk reconnaissance over the vault.
//
// Returns cross-tabs an agent can reason over to spot recurring conventions:
// frontmatter state machines (type x status), tag families (depth-3 tree),
// where each typed page lives (path x type), recent activity by region.
//
// Single page walk; per-page work is microsecond-level (cached field reads).
// Cardinality caps prevent UUID-style frontmatter and timestamp-leafed tags
// from drowning the result in noise.
//
// Placeholders:
//   __PATH_PREFIX__   - optional path prefix filter ("" = full vault)
//   __ACTIVITY_DAYS__ - lookback window in days for recent_activity_by_path
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const pathPrefix = `__PATH_PREFIX__`;
    const activityDays = parseInt('__ACTIVITY_DAYS__') || 30;
    const activityCutoffMs = Date.now() - activityDays * 86400000;

    const FM_VALUE_TOP_N = 50;
    const FM_VALUE_CARDINALITY_LIMIT = 100;
    const TAG_TREE_DEPTH = 3;
    const PATH_BY_TYPE_MIN_COUNT = 2;
    const TOP_TAGS_LIMIT = 30;
    const TOP_FM_KEYS_LIMIT = 30;
    const PATH_PREFIXES_LIMIT = 30;

    const result = {
        snapshot_ts: new Date().toISOString(),
        path_prefix_filter: pathPrefix || null,
        activity_days: activityDays,
        object_types: {},
        pages_walked: 0,
        pages_total: 0
    };

    // Object type counts (full vault — these are aggregate counts, not filtered)
    const typeNames = ['@page', '@section', '@block', '@codeblock', '@list-item', '@task'];
    for (const t of typeNames) {
        try {
            const r = api.query(t);
            result.object_types[t] = Array.isArray(r) ? r.length : 0;
        } catch (e) {
            result.object_types[t] = {query_error: e.message};
        }
    }

    function serializeValue(v) {
        if (v === null || v === undefined) return null;
        if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
            return String(v);
        }
        if (Array.isArray(v)) {
            try { return JSON.stringify(v); } catch (_) { return '[unserializable]'; }
        }
        if (typeof v === 'object') {
            try { return JSON.stringify(v); } catch (_) { return '[object]'; }
        }
        return String(v);
    }

    function mtimeMs(p) {
        const m = p.$mtime;
        if (!m) return null;
        if (typeof m === 'number') return m;
        if (typeof m.toMillis === 'function') return m.toMillis();
        if (m.ts) return m.ts;
        return null;
    }

    try {
        const allPages = api.query('@page');
        result.pages_total = allPages.length;

        const pages = pathPrefix
            ? allPages.filter(p => p.$path && p.$path.startsWith(pathPrefix))
            : allPages;
        result.pages_walked = pages.length;

        const tagCounts = {};
        const fmKeys = {};
        const fmValues = {};
        const pathPrefixCounts = {};
        const tagTree = { _count: 0, children: {} };
        const typeByStatus = {};
        const pathByType = {};
        const recentByPath = {};

        for (const p of pages) {
            const tags = p.$tags || [];
            for (const t of tags) {
                const top = t.split('/').slice(0, 2).join('/');
                tagCounts[top] = (tagCounts[top] || 0) + 1;

                const segments = t.replace(/^#/, '').split('/').slice(0, TAG_TREE_DEPTH);
                let node = tagTree;
                for (const seg of segments) {
                    node.children[seg] = node.children[seg] || { _count: 0, children: {} };
                    node = node.children[seg];
                    node._count += 1;
                }
            }

            const fm = p.$frontmatter || {};
            const fmEntries = Object.entries(fm);
            for (const [k, vRaw] of fmEntries) {
                fmKeys[k] = (fmKeys[k] || 0) + 1;
                const v = serializeValue(vRaw);
                if (v !== null) {
                    fmValues[k] = fmValues[k] || {};
                    fmValues[k][v] = (fmValues[k][v] || 0) + 1;
                }
            }

            const pathParts = (p.$path || '').split('/');
            if (pathParts.length > 1) {
                pathPrefixCounts[pathParts[0]] = (pathPrefixCounts[pathParts[0]] || 0) + 1;
            }

            const fmType = fm.type;
            const fmStatus = fm.status;
            if (fmType !== undefined && fmType !== null) {
                const tKey = serializeValue(fmType);
                typeByStatus[tKey] = typeByStatus[tKey] || {};
                const sKey = (fmStatus !== undefined && fmStatus !== null)
                    ? serializeValue(fmStatus) : '(none)';
                typeByStatus[tKey][sKey] = (typeByStatus[tKey][sKey] || 0) + 1;

                const parentDir = pathParts.slice(0, -1).join('/');
                if (parentDir) {
                    pathByType[parentDir] = pathByType[parentDir] || {};
                    pathByType[parentDir][tKey] = (pathByType[parentDir][tKey] || 0) + 1;
                }
            }

            const ms = mtimeMs(p);
            if (ms && ms >= activityCutoffMs) {
                const d2 = pathParts.slice(0, 2).join('/');
                if (d2) {
                    recentByPath[d2] = (recentByPath[d2] || 0) + 1;
                }
            }
        }

        result.top_tags = Object.entries(tagCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, TOP_TAGS_LIMIT)
            .map(([tag, count]) => ({tag, count}));

        result.frontmatter_keys = Object.entries(fmKeys)
            .sort((a, b) => b[1] - a[1])
            .slice(0, TOP_FM_KEYS_LIMIT)
            .map(([key, count]) => ({key, count}));

        result.path_prefixes = Object.entries(pathPrefixCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, PATH_PREFIXES_LIMIT)
            .map(([prefix, count]) => ({prefix, count}));

        result.frontmatter_values = {};
        result.high_cardinality_keys = [];
        for (const [k, valueMap] of Object.entries(fmValues)) {
            const distinctCount = Object.keys(valueMap).length;
            if (distinctCount > FM_VALUE_CARDINALITY_LIMIT) {
                result.high_cardinality_keys.push({key: k, distinct_count: distinctCount});
                continue;
            }
            const values = Object.entries(valueMap)
                .sort((a, b) => b[1] - a[1])
                .slice(0, FM_VALUE_TOP_N)
                .map(([value, count]) => ({value, count}));
            result.frontmatter_values[k] = {
                values,
                distinct_count: distinctCount,
                truncated: distinctCount > FM_VALUE_TOP_N
            };
        }
        result.high_cardinality_keys.sort((a, b) => b.distinct_count - a.distinct_count);

        result.tag_tree = tagTree.children;
        result.type_by_status = typeByStatus;

        result.path_by_type = {};
        for (const [path, types] of Object.entries(pathByType)) {
            const total = Object.values(types).reduce((s, n) => s + n, 0);
            if (total >= PATH_BY_TYPE_MIN_COUNT) {
                result.path_by_type[path] = types;
            }
        }

        const recentSorted = {};
        Object.entries(recentByPath)
            .sort((a, b) => b[1] - a[1])
            .forEach(([k, v]) => { recentSorted[k] = v; });
        result.recent_activity_by_path = recentSorted;
    } catch (e) {
        result.walk_error = e.message;
    }

    try {
        const tasks = api.query('@task');
        const statuses = {};
        for (const t of tasks) {
            const s = t.$status || 'unknown';
            statuses[s] = (statuses[s] || 0) + 1;
        }
        result.task_statuses = statuses;
        result.tasks_total = tasks.length;
    } catch (_) {}

    return result;
})()
