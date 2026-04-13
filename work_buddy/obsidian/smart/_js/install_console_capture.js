// Install console interception buffer — idempotent, safe to call multiple times.
// Captures console.log/warn/error/debug from all plugins into a ring buffer.
return (() => {
    if (window.__wb_console_buffer) return 'already installed';

    window.__wb_console_buffer = [];
    window.__wb_console_max = 500;

    const orig = {
        log: console.log.bind(console),
        warn: console.warn.bind(console),
        error: console.error.bind(console),
        debug: console.debug.bind(console),
    };
    window.__wb_console_orig = orig;

    for (const level of ['log', 'warn', 'error', 'debug']) {
        console[level] = (...args) => {
            const buf = window.__wb_console_buffer;
            buf.push({
                ts: Date.now(),
                level,
                msg: args.map(a => {
                    try { return typeof a === 'string' ? a : JSON.stringify(a); }
                    catch { return String(a); }
                }).join(' ')
            });
            if (buf.length > window.__wb_console_max) {
                buf.splice(0, buf.length - window.__wb_console_max);
            }
            orig[level](...args);
        };
    }
    return 'installed';
})()
