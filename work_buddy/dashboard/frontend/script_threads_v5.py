"""Threads tab — v5 dashboard surface (Stage 4 scaffold).

The Threads tab is the canonical surface for v5 Threads (UX.md). It
replaces the v4 tabs (Today / Engage / Review / Review Queue /
Daily Log) with one recursive UI.

Stage 4.0 ships a scaffold:
- The tab is registered with the dashboard but renders a
  placeholder ("Stage 4 in progress").
- All sub-stage content (URL routing, card layout, search, etc.)
  layers on top in subsequent commits.

Per UX.md §15, the implementation lands in 4.1 → 4.16 over
multiple commits.
"""

from __future__ import annotations


def _threads_v5_script() -> str:
    """Top-level Threads tab JS. Wires loadThreads into staticLoaders.

    Stage 4.0: empty implementation that paints a placeholder.
    Stage 4.1 wires URL routing.
    Stage 4.2+ adds card layouts.
    """
    return r"""
// ---------------------------------------------------------------------------
// Threads tab v5 — Stage 4 scaffold
// ---------------------------------------------------------------------------
//
// Public surface: window.loadThreads(opts) — called by switchTab in
// script_main.py's staticLoaders.
//
// Stage 4.0: paints a placeholder. Subsequent stages (4.1+) extend
// this scaffold.

(function () {
    if (typeof window.loadThreads === "function") {
        // Already defined in a hot-reload — bail.
        return;
    }

    window.loadThreads = function loadThreads(_opts) {
        const panel = document.getElementById("threads-panel");
        if (!panel) return;
        panel.innerHTML = `
            <div class="threads-v5-placeholder">
                <h2>Threads</h2>
                <p>v5 Threads surface — Stage 4 in progress.</p>
                <p class="threads-v5-stage-note">
                    The full UI lands across Stage 4 sub-stages.
                    Currently this is a scaffold; routing, cards,
                    search, and per-state interactions land in
                    successive commits per <code>UX.md §15</code>.
                </p>
            </div>
        `;
    };

    // Register with staticLoaders (defined in script_main.py). Falls
    // back gracefully if the loader map isn't initialised yet.
    try {
        if (typeof window.staticLoaders === "object" && window.staticLoaders) {
            window.staticLoaders.threads = window.loadThreads;
        }
    } catch (e) {
        // Non-fatal — staticLoaders may not be defined yet during the
        // first script-execution pass; script_main.py picks up
        // window.loadThreads directly via _initFromHash on next load.
    }
})();
"""


def _threads_v5_styles() -> str:
    return r"""
.threads-v5-placeholder {
    max-width: 720px;
    margin: 3em auto;
    padding: 1.5em 2em;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    color: var(--text, #ddd);
}

.threads-v5-placeholder h2 {
    margin-top: 0;
    color: var(--text, #ddd);
}

.threads-v5-stage-note {
    color: var(--text-muted, #888);
    font-size: 13px;
    margin-top: 1em;
}

.threads-v5-stage-note code {
    background: var(--bg-tertiary, #2a2a2a);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 12px;
}
"""
