// Validate a Datacore query string without executing it.
// Placeholder: __QUERY__
return (async () => {
    const api = window.datacore || app.plugins.plugins['datacore']?.api;
    if (!api) return {error: 'Datacore API not available'};

    const queryStr = `__QUERY__`;
    const result = api.tryParseQuery(queryStr);

    if (result.successful) {
        return {valid: true, parsed: {type: result.value?.type}};
    } else {
        return {valid: false, parse_error: String(result.error)};
    }
})()
