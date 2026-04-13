// Compute hot-file scores for a time window.
// __SINCE_DATE__ = "YYYY-MM-DD" (inclusive)
// __UNTIL_DATE__ = "YYYY-MM-DD" (inclusive)
// __LIMIT__ = max files to return
return (async () => {
    const plugin = app.plugins.plugins["keep-the-rhythm"];
    if (!plugin) return {error: "Plugin not found"};

    const activities = plugin.data?.stats?.dailyActivity;
    if (!activities) return {error: "No activity data"};

    const sinceDate = "__SINCE_DATE__";
    const untilDate = "__UNTIL_DATE__";
    const limit = __LIMIT__;

    // Filter to window
    const inWindow = activities.filter(a =>
        a.date >= sinceDate && a.date <= untilDate
    );

    // Aggregate per file
    const fileStats = {};
    for (const a of inWindow) {
        const fp = a.filePath;
        if (!fileStats[fp]) {
            fileStats[fp] = {
                filePath: fp,
                active_days: 0,
                total_buckets: 0,
                total_word_delta: 0,
                total_char_delta: 0,
                last_active_date: "",
                last_active_time: "",
                dates: new Set(),
            };
        }
        const fs = fileStats[fp];
        fs.dates.add(a.date);
        if (a.changes) {
            for (const c of a.changes) {
                fs.total_buckets++;
                fs.total_word_delta += Math.abs(c.w || 0);
                fs.total_char_delta += Math.abs(c.c || 0);
                // Track latest activity time
                const dt = a.date + "T" + c.timeKey;
                if (dt > (fs.last_active_date + "T" + fs.last_active_time)) {
                    fs.last_active_date = a.date;
                    fs.last_active_time = c.timeKey;
                }
            }
        }
        // Even without changes, file was opened
        if (a.date > fs.last_active_date) {
            fs.last_active_date = a.date;
        }
    }

    // Compute scores and convert Sets
    const now = new Date();
    const results = Object.values(fileStats).map(fs => {
        fs.active_days = fs.dates.size;
        delete fs.dates;
        // Score: weighted combination of recency, frequency, and intensity
        const daysSinceActive = Math.max(0,
            (now - new Date(fs.last_active_date + "T23:59:59")) / 86400000
        );
        const recency = 1 / (1 + daysSinceActive);   // 0-1, higher = more recent
        const frequency = fs.active_days;              // more days = hotter
        const intensity = fs.total_buckets;            // more buckets = hotter
        fs.hot_score = Math.round(
            (recency * 50 + frequency * 10 + intensity * 5) * 100
        ) / 100;
        return fs;
    });

    // Sort by hot_score descending, take top N
    results.sort((a, b) => b.hot_score - a.hot_score);
    return {
        window: {since: sinceDate, until: untilDate},
        total_files: results.length,
        files: results.slice(0, limit),
    };
})()
