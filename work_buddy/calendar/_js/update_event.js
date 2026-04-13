// Update an existing Google Calendar event.
// Params: __EVENT_JSON__ (full event object as JSON string), __NOTIFY__
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    let event;
    try {
        event = JSON.parse('__EVENT_JSON__');
    } catch (e) {
        return {error: 'Invalid event JSON: ' + e.message};
    }

    if (!event.id) return {error: 'Event must have an id field'};

    const notify = '__NOTIFY__' === 'true';

    try {
        const updated = await plugin.api.updateEvent(event, notify);
        if (!updated) return {error: 'updateEvent returned null'};
        return {
            success: true,
            id: updated.id,
            summary: updated.summary,
            start: updated.start,
            end: updated.end,
            htmlLink: updated.htmlLink,
            notified: notify
        };
    } catch (e) {
        return {error: 'updateEvent failed: ' + e.message};
    }
})()
