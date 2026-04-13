// Drain and return all buffered console messages, optionally filtered.
// Params: __SINCE_TS__ (epoch ms, 0 for all), __LEVEL_FILTER__ ('all' or 'error'/'warn'/'log')
return (() => {
    const buf = window.__wb_console_buffer;
    if (!buf) return {error: 'Console capture not installed. Call install_console_capture first.'};

    const since = __SINCE_TS__;
    const levelFilter = '__LEVEL_FILTER__';

    let entries;
    if (since > 0) {
        const idx = buf.findIndex(e => e.ts >= since);
        entries = idx === -1 ? [] : buf.splice(idx);
    } else {
        entries = buf.splice(0);
    }

    if (levelFilter !== 'all') {
        const levels = levelFilter === 'error' ? ['error'] : levelFilter === 'warn' ? ['warn', 'error'] : [levelFilter];
        entries = entries.filter(e => levels.includes(e.level));
    }

    return entries;
})()
