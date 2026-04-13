// Check if the Day Planner plugin is loaded and return key settings.
return (async () => {
    const plugin = app.plugins.plugins["obsidian-day-planner"];
    if (!plugin) return {ready: false, reason: "Day Planner plugin not found"};

    if (!plugin._loaded) return {ready: false, reason: "Plugin not yet loaded"};

    // Read settings from the Svelte store
    let settings = null;
    try {
        const p = new Promise(resolve => {
            const unsub = plugin.settingsStore.subscribe(val => {
                resolve(val);
                if (unsub) unsub();
            });
        });
        settings = await p;
    } catch (e) {
        return {ready: false, reason: "Could not read settings: " + e.message, version: plugin.manifest.version};
    }

    // Check if remote calendars are configured
    const hasRemoteCalendars = (settings.icals && settings.icals.length > 0)
        || (settings.rawIcals && settings.rawIcals.length > 0);

    return {
        ready: true,
        version: plugin.manifest.version,
        plannerHeading: settings.plannerHeading,
        plannerHeadingLevel: settings.plannerHeadingLevel,
        timestampFormat: settings.timestampFormat,
        defaultDurationMinutes: settings.defaultDurationMinutes,
        startHour: settings.startHour,
        snapStepMinutes: settings.snapStepMinutes,
        hasRemoteCalendars: hasRemoteCalendars,
        showTimeTracker: settings.showTimeTracker
    };
})()
