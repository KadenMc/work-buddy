// Read SmartEnv event_logs collection.
// Params: __UNSEEN_ONLY__ (true/false), __CATEGORY__ ('all' or category prefix), __LIMIT__
return (() => {
    const el = window.smart_env?.event_logs;
    if (!el) return {error: 'No event_logs on smart_env'};

    const unseenOnly = __UNSEEN_ONLY__;
    const category = '__CATEGORY__';
    const limit = __LIMIT__;

    let entries;
    if (unseenOnly && typeof el.get_unseen_notification_entries === 'function') {
        entries = el.get_unseen_notification_entries() || [];
    } else {
        entries = Object.values(el.items || {});
    }

    // Map to serializable format
    let mapped = entries.map(item => {
        const d = item?.data || item || {};
        const key = d.key || '';
        const fmt = (ms) => ms ? new Date(ms).toISOString().replace('T', ' ').slice(0, 19) : null;
        return {
            key: key,
            category: key.split(':')[0] || 'unknown',
            ct: d.ct || 0,
            first_at: fmt(d.first_at),
            last_at: fmt(d.last_at),
            first_at_ms: d.first_at || null,
            last_at_ms: d.last_at || null,
            sources: d.event_sources ? Object.keys(d.event_sources) : []
        };
    });

    // Filter by category
    if (category !== 'all') {
        mapped = mapped.filter(e => e.category === category);
    }

    // Sort by last_at descending
    mapped.sort((a, b) => (b.last_at || 0) - (a.last_at || 0));

    // Count errors
    const errorCount = Object.keys(el.items || {}).filter(k => k.includes('error')).reduce((sum, k) => {
        return sum + (el.items[k]?.data?.ct || 0);
    }, 0);

    // Categories summary
    const cats = {};
    for (const e of Object.values(el.items || {})) {
        const c = (e?.data?.key || '').split(':')[0];
        cats[c] = (cats[c] || 0) + 1;
    }

    return {
        total_types: Object.keys(el.items || {}).length,
        categories: cats,
        error_count: errorCount,
        unseen_count: typeof el.get_unseen_notification_count === 'function' ? el.get_unseen_notification_count() : null,
        entries: mapped.slice(0, limit)
    };
})()
