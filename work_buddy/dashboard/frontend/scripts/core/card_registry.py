"""Dashboard card-registry frontend infrastructure.

The server half of the card pattern lives in ``work_buddy/dashboard/
cards.py``; this is the client half. It owns ``window.wbCardRenderers``
(the ``card id → renderer`` map that card modules populate) and the
generic ``window.wbMountCards`` mounter.

A card module registers its renderer at script-load time:

    window.wbCardRenderers['obsidian.bridge_sparkline'] =
        function(state) { return '<div>…</div>'; };

A renderer may be synchronous (returns an HTML string) or asynchronous
(returns a Promise of one) — the mounter awaits either.

Concatenated after ``core/helpers`` (uses ``fetchJSON``) and before any
card module. See ``architecture/feature-cards``.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Card registry + generic mounter ----
window.wbCardRenderers = window.wbCardRenderers || {};

// Mount the gated cards for a mount point into a container.
//
// Fetches the active card list from /api/dashboard/cards/<mountPoint>
// (the server evaluates each card's gate against the control graph),
// runs each registered renderer, and morphdom-merges the result so
// user state inside surviving cards is preserved. Cards whose gating
// component was opted out are simply absent from the server's list and
// therefore vanish from the DOM — no placeholder.
window.wbMountCards = async function(mountPoint, container, state) {
    if (!container) return;
    const listing = await fetchJSON('/api/dashboard/cards/' + mountPoint);
    if (!listing || !Array.isArray(listing.cards)) return;
    const parts = await Promise.all(listing.cards.map(async (c) => {
        const fn = window.wbCardRenderers[c.id];
        if (typeof fn !== 'function') {
            console.warn('[cards] no renderer registered for', c.id);
            return '';
        }
        try {
            return (await fn(state)) || '';
        } catch (e) {
            console.error('[cards] renderer threw for', c.id, e);
            return '';
        }
    }));
    const html = parts.join('');
    if (typeof window._wbMorphReplace === 'function') {
        window._wbMorphReplace(container, html);
    } else {
        container.innerHTML = html;
    }
};
"""
