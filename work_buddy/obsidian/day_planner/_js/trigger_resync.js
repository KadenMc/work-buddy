// Trigger Day Planner's re-sync command to refresh timeline after writes.
return (async () => {
    const plugin = app.plugins.plugins["obsidian-day-planner"];
    if (!plugin) return {error: "Day Planner plugin not found"};

    try {
        await app.commands.executeCommandById("obsidian-day-planner:re-sync");
        return {success: true, message: "Day Planner resync triggered"};
    } catch (e) {
        return {error: "Resync failed: " + e.message};
    }
})()
