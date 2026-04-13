// Create a new Google Calendar event.
// Params: __SUMMARY__, __START__, __END__, __CALENDAR_ID__, __DESCRIPTION__, __LOCATION__, __ALL_DAY__
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    const isAllDay = '__ALL_DAY__' === 'true';

    const event = {
        summary: '__SUMMARY__',
        parent: {id: '__CALENDAR_ID__'}
    };

    if ('__DESCRIPTION__') event.description = '__DESCRIPTION__';
    if ('__LOCATION__') event.location = '__LOCATION__';

    if (isAllDay) {
        event.start = {date: '__START__'};
        event.end = {date: '__END__'};
    } else {
        event.start = {dateTime: '__START__', timeZone: '__TIMEZONE__'};
        event.end = {dateTime: '__END__', timeZone: '__TIMEZONE__'};
    }

    try {
        const created = await plugin.api.createEvent(event);
        if (!created) return {error: 'createEvent returned null'};
        return {
            success: true,
            id: created.id,
            summary: created.summary,
            start: created.start,
            end: created.end,
            htmlLink: created.htmlLink,
            calendarId: created.parent?.id || '__CALENDAR_ID__'
        };
    } catch (e) {
        return {error: 'createEvent failed: ' + e.message};
    }
})()
