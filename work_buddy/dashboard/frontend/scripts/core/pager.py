"""Shared page-based pager renderer — used by every tab that needs
to chunk a large result set into fixed-size pages.

JS API::

    wbRenderPager(containerId, total, currentPage, pageSize, onPageFnName)

Renders into ``#<containerId>``:

- ``«`` / ``»`` previous-next chevrons (disabled at the ends),
- a sliding window of numbered buttons (first, last, current ± 2,
  with ellipses for the gaps),
- a small "N–M of Total" info span.

``onPageFnName`` is the *string name* of a global handler that the
buttons call with the new page number — i.e. the buttons do
``onclick="<name>(N)"``. The handler is the caller's responsibility
(typically: stash the new page on tab state, invalidate the cache,
trigger a re-render). The pager is purely a renderer; it never
mutates state itself.

Hides itself (empties the container) when ``total <= pageSize``.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ===========================================================================
// Shared pager — wbRenderPager
// ===========================================================================
window.wbRenderPager = function (containerId, total, currentPage, pageSize, onPageFnName) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (total <= pageSize) { el.innerHTML = ''; return; }
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const cur = Math.min(Math.max(currentPage, 1), totalPages);
    const startIdx = (cur - 1) * pageSize + 1;
    const endIdx = Math.min(cur * pageSize, total);

    function _esc(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function pageBtn(n, label, opts) {
        opts = opts || {};
        const classes = ['wb-pager-btn'];
        if (opts.current) classes.push('current');
        const disabled = opts.disabled ? ' disabled' : '';
        const onClick = opts.disabled ? '' : ` onclick="${onPageFnName}(${n})"`;
        return `<button class="${classes.join(' ')}"${disabled}${onClick}>${_esc(label)}</button>`;
    }

    let html = '';
    html += pageBtn(cur - 1, '‹', { disabled: cur === 1 });

    // First / last + a sliding window of cur ± 2.
    const pages = new Set([1, totalPages, cur, cur - 1, cur + 1, cur - 2, cur + 2]);
    const visible = Array.from(pages)
        .filter(n => n >= 1 && n <= totalPages)
        .sort((a, b) => a - b);
    let prev = 0;
    for (const n of visible) {
        if (n - prev > 1) html += '<span class="wb-pager-ellipsis">…</span>';
        html += pageBtn(n, String(n), { current: n === cur });
        prev = n;
    }
    html += pageBtn(cur + 1, '›', { disabled: cur === totalPages });
    html += `<span class="wb-pager-info">${startIdx}–${endIdx} of ${total}</span>`;
    el.innerHTML = html;
};
"""
