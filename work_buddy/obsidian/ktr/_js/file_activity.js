// Get detailed writing activity for a specific file within a time window.
// __FILE_PATH__ = vault-relative path (e.g. "journal/2026-04-04.md")
// __SINCE_DATE__ = "YYYY-MM-DD" (inclusive)
// __UNTIL_DATE__ = "YYYY-MM-DD" (inclusive)
return (async () => {
    const plugin = app.plugins.plugins["keep-the-rhythm"];
    if (!plugin) return {error: "Plugin not found"};

    const activities = plugin.data?.stats?.dailyActivity;
    if (!activities) return {error: "No activity data"};

    const filePath = "__FILE_PATH__";
    const sinceDate = "__SINCE_DATE__";
    const untilDate = "__UNTIL_DATE__";

    const records = activities.filter(a =>
        a.filePath === filePath &&
        a.date >= sinceDate && a.date <= untilDate
    );

    if (records.length === 0) {
        return {filePath, found: false, records: []};
    }

    return {
        filePath,
        found: true,
        records: records.map(r => ({
            date: r.date,
            word_count_start: r.wordCountStart,
            char_count_start: r.charCountStart,
            changes: r.changes || [],
            total_word_delta: (r.changes || []).reduce((s, c) => s + (c.w || 0), 0),
            total_char_delta: (r.changes || []).reduce((s, c) => s + (c.c || 0), 0),
        })),
    };
})()
