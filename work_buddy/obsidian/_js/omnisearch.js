// Lexical search via Omnisearch.
// Params: __TEXT__, __LIMIT__ (replaced by Python)
return (async () => {
    const omni = globalThis.omnisearch || app?.plugins?.plugins?.omnisearch?.api;
    if (!omni?.search) return {error: 'Omnisearch not available'};

    try {
        const results = await omni.search('__TEXT__');
        return {
            results: results.slice(0, __LIMIT__).map(r => ({
                path: r.path,
                score: r.score,
                excerpt: (r.excerpt || '').slice(0, 300),
                matches: r.matches,
                foundWords: r.foundWords
            }))
        };
    } catch (e) {
        return {error: 'Omnisearch failed: ' + e.message};
    }
})()
