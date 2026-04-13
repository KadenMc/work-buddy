// Check if Keep the Rhythm plugin is loaded and has activity data.
return (async () => {
    const plugin = app.plugins.plugins["keep-the-rhythm"];
    if (!plugin) return {ready: false, reason: "Keep the Rhythm plugin not found"};

    const data = plugin.data;
    if (!data?.stats?.dailyActivity) {
        return {ready: false, reason: "No activity data found"};
    }

    return {
        ready: true,
        version: plugin.manifest?.version || "unknown",
        activity_count: data.stats.dailyActivity.length,
        unique_files: [...new Set(data.stats.dailyActivity.map(a => a.filePath))].length,
        current_streak: data.stats.currentStreak || 0,
    };
})()
