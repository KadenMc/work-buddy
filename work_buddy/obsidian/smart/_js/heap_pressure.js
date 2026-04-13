// Focused heap pressure check — deliberately minimal to avoid allocating memory.
return (() => {
    const pm = performance.memory;
    const nm = typeof process !== 'undefined' ? process.memoryUsage() : null;
    const used = pm ? pm.usedJSHeapSize : 0;
    const limit = pm ? pm.jsHeapSizeLimit : 0;
    const pct = limit ? Math.round(used / limit * 100) : -1;
    const status = pct < 0 ? 'unknown' : pct < 75 ? 'ok' : pct < 85 ? 'elevated' : pct < 93 ? 'warning' : 'critical';
    return {
        heap_used_mb: pm ? Math.round(used / 1048576) : null,
        heap_total_mb: pm ? Math.round(pm.totalJSHeapSize / 1048576) : null,
        heap_limit_mb: pm ? Math.round(limit / 1048576) : null,
        heap_percent: pct,
        rss_mb: nm ? Math.round(nm.rss / 1048576) : null,
        status: status
    };
})()
