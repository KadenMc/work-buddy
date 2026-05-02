"""Threads tab — v5 dashboard surface.

Stage 4.0 shipped a placeholder. Stage 4.1 wires:
- URL routing for nested thread paths and inspect modals.
- Recursive UI scaffold: top-level list, breadcrumbs, depth ≥ 1 detail.
- Stub list + card content (real card layouts land in 4.2+).

UX.md §2 (navigation) and §11 (URLs) are the spec.
"""

from __future__ import annotations


def _threads_v5_script() -> str:
    return r"""
// ===========================================================================
// Threads tab v5 — Stage 4.1 URL routing + recursive UI scaffold
// ===========================================================================

(function () {
    if (typeof window.loadThreads === "function") return;

    // ----- State ---------------------------------------------------------
    //
    // window._threadsState is the single source of truth. _persistHash
    // (in script_main.py) reads it to encode the URL; _initFromHash writes
    // it to seed the initial render.
    //
    // Shape:
    //   { path: ['th-abc', 'th-def', ...],  // depth-N nested
    //     inspect: 'ci-7' | 'ev-42' | null }
    if (!window._threadsState) {
        window._threadsState = { path: [], inspect: null };
    }

    function _esc(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // Public navigation helpers — exposed on window so other modules can
    // navigate (e.g., notification toast → "open in Threads tab").

    window.threadsPushPath = function (segment) {
        if (!segment) return;
        window._threadsState.path.push(segment);
        window._threadsState.inspect = null;  // clear inspector on nav
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsSetPath = function (parts) {
        window._threadsState.path = (parts || []).slice();
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsBack = function () {
        if (window._threadsState.path.length === 0) return;
        window._threadsState.path.pop();
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsOpenInspector = function (itemId) {
        if (!itemId) return;
        window._threadsState.inspect = itemId;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsCloseInspector = function () {
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    // ----- Rendering -----------------------------------------------------

    function renderThreads() {
        const panel = document.getElementById("panel-threads");
        if (!panel) return;
        const state = window._threadsState || { path: [], inspect: null };

        let html = "";
        html += renderBreadcrumbs(state.path);

        if (state.path.length === 0) {
            html += renderTopLevel();
        } else {
            html += renderThreadDetail(state.path[state.path.length - 1]);
        }
        if (state.inspect) {
            html += renderInspector(state.inspect);
        }
        panel.innerHTML = html;
    }

    function renderBreadcrumbs(path) {
        // Stage 4.1: minimal breadcrumb with back button. Polishing
        // (visual styling, click-to-jump-to-depth) lands in 4.2+.
        let html = '<nav class="threads-v5-breadcrumbs">';
        html += '<button class="threads-v5-back" '
              + 'onclick="threadsBack()" '
              + (path.length === 0 ? 'disabled' : '')
              + ' title="Back">&larr; Back</button>';
        html += '<span class="threads-v5-crumb-sep">Threads</span>';
        for (let i = 0; i < path.length; i++) {
            html += '<span class="threads-v5-crumb-sep">/</span>';
            const isLast = (i === path.length - 1);
            const segment = path[i];
            if (isLast) {
                html += '<span class="threads-v5-crumb threads-v5-crumb-current">'
                      + _esc(segment) + '</span>';
            } else {
                // Click to jump to this depth
                const subPath = path.slice(0, i + 1);
                const json = JSON.stringify(subPath).replace(/"/g, "&quot;");
                html += '<a href="#" class="threads-v5-crumb" '
                      + 'onclick="event.preventDefault();threadsSetPath('
                      + json + ')">' + _esc(segment) + '</a>';
            }
        }
        html += '</nav>';
        return html;
    }

    function renderTopLevel() {
        // Stage 4.1: scaffold the top-level list view. Stage 4.2+ adds
        // real card layouts. For now: a placeholder "no Threads yet"
        // message + a stub list of any threads we can find via the
        // upcoming /api/threads endpoint (Stage 4.3 wires that).
        return (
            '<div class="threads-v5-top">'
            + '<h2>Threads</h2>'
            + '<p class="threads-v5-stage-note">'
            + 'Stage 4.1 wired URL routing. Cards + real list rendering '
            + 'land in Stage 4.2+. Click any "Open thread" demo button '
            + 'below to test recursive navigation.'
            + '</p>'
            + '<div class="threads-v5-demo-row">'
            +   '<button class="threads-v5-demo-btn" '
            +     'onclick="threadsPushPath(\'th-demo123\')">Open demo Thread</button>'
            +   '<button class="threads-v5-demo-btn" '
            +     'onclick="threadsPushPath(\'th-demo456\')">Open another demo Thread</button>'
            + '</div>'
            + '</div>'
        );
    }

    function renderThreadDetail(threadId) {
        // Stage 4.2: real card layout. Backend wiring (4.3) replaces
        // the mock thread data with /api/threads/<id> response.
        if (typeof window.renderConfirmationCard !== "function") {
            return '<div class="threads-v5-detail">'
                 + '<p>Card module not loaded.</p></div>';
        }
        const thread = _mockThread(threadId);
        return window.renderConfirmationCard(thread);
    }

    // Stage 4.2: stub thread data so the card renders end-to-end
    // before backend wiring (4.3). Replaced by an /api/threads
    // fetch in 4.3.
    function _mockThread(threadId) {
        return {
            thread_id: threadId,
            title: "Sarah's birthday + gift",
            urgency: "defer",
            fsm_state: "awaiting_confirmation",
            intent: { text: "Schedule Sarah's 30th birthday + buy a gift.", editable: true },
            context_items: [
                { id: "ci-1", label: "Sarah's note (running journal)",
                  source: "journal_note", type: "todo_line",
                  payload: { line: "Sarah's birthday May 12" } },
                { id: "ci-2", label: "Calendar destination: Personal",
                  source: "calendar", type: "calendar",
                  payload: { calendar_id: "personal" } },
            ],
            actions: [
                { id: "act-1", name: "create_calendar_event", kind: "standard",
                  parameters: { title: "Sarah's 30th birthday",
                               datetime: "2026-05-12T18:00:00",
                               duration_minutes: 60 },
                  plan_summary: "Sarah's 30th birthday • 2026-05-12 18:00",
                  required_contexts: ["@calendar"] },
                { id: "act-2", name: "create_task", kind: "standard",
                  parameters: { description: "Buy gift for Sarah",
                               due_date: "2026-05-12" },
                  plan_summary: "Buy gift for Sarah • due 2026-05-12",
                  required_contexts: [] },
            ],
            namespace_tags: [],
            can_clean_up: false,
            sub_thread_count: 0,
        };
    }

    function renderInspector(itemId) {
        // Stage 4.1: minimal modal scaffold. Stage 4.5 + 4.6 implement
        // the real per-item-type modals (context items, actions, events).
        return (
            '<div class="threads-v5-modal-backdrop" '
            +   'onclick="threadsCloseInspector()">'
            + '<div class="threads-v5-modal" onclick="event.stopPropagation()">'
            +   '<div class="threads-v5-modal-header">'
            +     '<span>Inspector: ' + _esc(itemId) + '</span>'
            +     '<button onclick="threadsCloseInspector()" '
            +       'class="threads-v5-modal-close">&times;</button>'
            +   '</div>'
            +   '<div class="threads-v5-modal-body">'
            +     '<p>Stage 4.1 — inspector scaffold.</p>'
            +     '<p>The real per-item-type modals land in 4.5/4.6.</p>'
            +   '</div>'
            + '</div>'
            + '</div>'
        );
    }

    window.loadThreads = function (_opts) {
        // Apply any state seeded by _initFromHash (script_main.py)
        renderThreads();
    };

    // Exposed so card-state mutators (in script_threads_v5_card.py)
    // can trigger a re-render after toggling X-flags or changing
    // focus, without re-implementing renderThreads here.
    window._renderActiveThread = renderThreads;

    // Register with staticLoaders if available
    try {
        if (typeof window.staticLoaders === "object" && window.staticLoaders) {
            window.staticLoaders.threads = window.loadThreads;
        }
    } catch (e) { /* deferred to script_main's pre-registration */ }

    // Hashchange listener: when the user uses browser back/forward, the
    // hash changes; re-extract state and re-render iff currently on the
    // Threads tab. (script_main.py already calls _initFromHash on
    // pageload; this handles in-session hash changes.)
    window.addEventListener("hashchange", function () {
        const params = new URLSearchParams(
            (window.location.hash || "").replace(/^#/, "")
        );
        if (params.get("tab") === "threads") {
            const tpath = params.get("tpath") || "";
            window._threadsState = {
                path: tpath ? tpath.split("/").filter(Boolean) : [],
                inspect: params.get("inspect") || null,
            };
            renderThreads();
        }
    });
})();
"""


def _threads_v5_styles() -> str:
    return r"""
/* Stage 4.1 — placeholder + breadcrumbs + inspector modal scaffold */

#panel-threads {
    padding: 0;
}

.threads-v5-placeholder {
    max-width: 720px;
    margin: 3em auto;
    padding: 1.5em 2em;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
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

/* Breadcrumb bar */
.threads-v5-breadcrumbs {
    padding: 12px 20px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--bg-secondary, #1a1a1a);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-muted, #888);
}

.threads-v5-back {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
    margin-right: 8px;
}

.threads-v5-back:disabled {
    opacity: 0.4;
    cursor: default;
}

.threads-v5-crumb {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
    font-family: var(--font-mono, monospace);
}

.threads-v5-crumb:hover {
    text-decoration: underline;
}

.threads-v5-crumb-current {
    color: var(--text, #ddd);
    font-family: var(--font-mono, monospace);
    font-weight: 600;
}

.threads-v5-crumb-sep {
    color: var(--text-muted, #666);
}

/* Top-level + detail panes */
.threads-v5-top, .threads-v5-detail {
    max-width: 900px;
    margin: 2em auto;
    padding: 1.5em 2em;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    color: var(--text, #ddd);
}

.threads-v5-top h2, .threads-v5-detail h2 {
    margin-top: 0;
}

.threads-v5-demo-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 1em;
}

.threads-v5-demo-btn {
    background: var(--accent, #4a7fc1);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
    cursor: pointer;
}

.threads-v5-demo-btn:hover {
    filter: brightness(1.1);
}

/* Modal scaffold */
.threads-v5-modal-backdrop {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
}

.threads-v5-modal {
    background: var(--bg, #0d0d0d);
    border: 1px solid var(--border, #333);
    border-radius: 10px;
    width: 80%;
    max-width: 640px;
    max-height: 80%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.threads-v5-modal-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border, #333);
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--text, #ddd);
    font-weight: 600;
}

.threads-v5-modal-close {
    background: transparent;
    color: var(--text-muted, #888);
    border: none;
    font-size: 24px;
    cursor: pointer;
    line-height: 1;
}

.threads-v5-modal-body {
    padding: 18px 20px;
    color: var(--text, #ddd);
    overflow-y: auto;
}
"""
