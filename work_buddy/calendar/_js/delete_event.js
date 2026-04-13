// Delete a Google Calendar event.
// Params: __EVENT_JSON__ (event object with at least id and parent.id), __NOTIFY__
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
        await plugin.api.deleteEvent(event, notify);
        return {
            success: true,
            deleted_id: event.id,
            summary: event.summary || '(unknown)',
            notified: notify
        };
    } catch (e) {
        return {error: 'deleteEvent failed: ' + e.message};
    }
})()
