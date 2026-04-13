"""Dashboard CSS styles."""

from __future__ import annotations


def _styles() -> str:
    return """
:root {
    --bg-primary: #0d1117;
    --bg-secondary: #161b22;
    --bg-tertiary: #21262d;
    --border: #30363d;
    --text-primary: #e6edf3;
    --text-secondary: #8b949e;
    --text-muted: #6e7681;
    --accent: #D87857;
    --accent-subtle: #D8785733;
    --green: #3fb950;
    --green-subtle: #238636;
    --yellow: #d29922;
    --red: #f85149;
    --orange: #db6d28;
    --purple: #bc8cff;
    --purple-subtle: #bc8cff22;
    --note-color: var(--accent);
    --note-subtle: var(--accent-subtle);
    --request-color: var(--purple);
    --request-subtle: var(--purple-subtle);
    /* Semantic button tokens */
    --btn-approve:        #3fb950;
    --btn-approve-subtle: #3fb9501a;
    --btn-approve-mid:    #3fb95033;
    --btn-deny:           #f85149;
    --btn-deny-subtle:    #f851491a;
    --btn-deny-mid:       #f8514933;
    --btn-neutral:        #58a6ff;
    --btn-neutral-subtle: #58a6ff1a;
    --btn-neutral-mid:    #58a6ff33;
    --btn-request-mid:    #bc8cff33;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    line-height: 1.5;
    min-height: 100vh;
}

/* Dark-themed scrollbars */
* {
    scrollbar-width: thin;
    scrollbar-color: var(--bg-tertiary) transparent;
}
*:hover {
    scrollbar-color: var(--border) transparent;
}
::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}
::-webkit-scrollbar-track {
    background: transparent;
}
::-webkit-scrollbar-thumb {
    background: var(--bg-tertiary);
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: var(--border);
}
::-webkit-scrollbar-corner {
    background: transparent;
}

/* -- Header ------------------------------------------------------------ */

.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
}

.header h1 {
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
}

.header h1 span { color: var(--accent); }

.header-meta {
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: var(--text-secondary);
}

.status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 4px;
    vertical-align: middle;
}

.status-dot.healthy { background: var(--green); }
.status-dot.unhealthy { background: var(--yellow); }
.status-dot.stopped { background: var(--text-muted); }
.status-dot.crashed { background: var(--red); }

/* -- Tab bar ----------------------------------------------------------- */

.tab-bar {
    display: flex;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
    padding: 0 24px;
    overflow-x: auto;
}
.tab-bar-left, .tab-bar-right { display: flex; gap: 0; }

.tab-btn {
    padding: 10px 16px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.15s, border-color 0.15s;
}

.tab-btn:hover { color: var(--text-primary); }
.tab-btn.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}

/* -- Panels ------------------------------------------------------------ */

.tab-panel {
    display: none;
    padding: 24px;
    max-width: 1200px;
    margin: 0 auto;
}

.tab-panel.active { display: block; }

/* -- Cards ------------------------------------------------------------- */

.card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
}

.card {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
}

.card-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted);
    margin-bottom: 4px;
}

.card-value {
    font-size: 28px;
    font-weight: 700;
    color: var(--text-primary);
}

.card-value.small { font-size: 18px; }
.card-value .unit {
    font-size: 14px;
    font-weight: 400;
    color: var(--text-secondary);
}

/* -- Tables ------------------------------------------------------------ */

.data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}

.data-table th {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.data-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text-primary);
}

.data-table tr:hover td { background: var(--bg-tertiary); }

/* -- Badges ------------------------------------------------------------ */

.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
}

.badge-green { background: var(--green-subtle); color: var(--green); }
.badge-yellow { background: #d299221a; color: var(--yellow); }
.badge-red { background: #f851491a; color: var(--red); }
.badge-blue { background: var(--accent-subtle); color: var(--accent); }
.badge-purple { background: #bc8cff1a; color: var(--purple); }
.badge-muted { background: var(--bg-tertiary); color: var(--text-secondary); }

/* -- Section headings -------------------------------------------------- */

.section-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
}

/* -- Loading / empty --------------------------------------------------- */

.loading, .empty-state {
    text-align: center;
    padding: 48px 16px;
    color: var(--text-secondary);
    font-size: 13px;
}

/* -- Bridge card ------------------------------------------------------- */

.bridge-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
}

.bridge-header {
    display: flex;
    align-items: center;
    gap: 24px;
    flex-wrap: wrap;
}

.bridge-header h3 {
    font-size: 14px;
    font-weight: 600;
    margin: 0;
    white-space: nowrap;
}

.bridge-stats {
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: var(--text-secondary);
}

.bridge-stat-value {
    font-weight: 700;
    color: var(--text-primary);
}

.bridge-meta {
    margin-left: auto;
    font-size: 11px;
    color: var(--text-muted);
}

.bridge-sparkline {
    height: 36px;
    display: flex;
    align-items: flex-end;
    gap: 2px;
    margin-top: 8px;
}

.bridge-sparkline .bar {
    flex: 1;
    min-width: 3px;
    max-width: 8px;
    border-radius: 2px 2px 0 0;
    transition: height 0.3s;
}

.bar-ok { background: var(--green); opacity: 0.7; }
.bar-ok:hover { opacity: 1; }
.bar-slow { background: var(--yellow); opacity: 0.7; }
.bar-slow:hover { opacity: 1; }
.bar-fail { background: var(--red); opacity: 0.7; }
.bar-fail:hover { opacity: 1; }

/* -- Health tree ------------------------------------------------------- */

.health-summary {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 12px;
}

.health-bar {
    height: 6px;
    background: var(--bg-tertiary);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 8px;
}

.health-bar-fill {
    height: 100%;
    background: var(--green);
    border-radius: 3px;
    transition: width 0.5s ease;
}

.health-counts {
    display: flex;
    gap: 16px;
    font-size: 12px;
}

.health-count.healthy { color: var(--green); }
.health-count.unhealthy { color: var(--yellow); }
.health-count.disabled { color: var(--text-muted); }

.health-item {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 6px;
    overflow: hidden;
}

.health-row {
    padding: 10px 16px;
}

.health-row.issue { background: #f851490a; }

.health-row-main {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.health-name {
    font-size: 13px;
    font-weight: 500;
}

.health-chevron {
    font-size: 10px;
    color: var(--text-muted);
    transition: transform 0.2s;
    cursor: pointer;
}

.health-item.collapsed .health-chevron { transform: rotate(-90deg); }
.health-item.collapsed .health-sub { display: none; }
.health-item.collapsed .health-diag { display: none; }

/* Clickable header row for expandable items */
.health-item > .health-row { cursor: pointer; user-select: none; }
.health-item > .health-row:hover { background: var(--bg-tertiary); }

.health-sub-count {
    font-size: 11px;
    color: var(--text-muted);
}

.health-sub-count.warn { color: var(--yellow); }

.health-sub {
    border-top: 1px solid var(--border);
}

.health-row.sub {
    padding: 8px 16px 8px 36px;
    border-bottom: 1px solid var(--border);
}

.health-row.sub:last-child { border-bottom: none; }

.health-row-detail {
    display: flex;
    gap: 6px;
    margin-top: 4px;
    padding-left: 12px;
    flex-wrap: wrap;
}

.health-chip {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 3px;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
}

.health-chip.warn { background: #d299221a; color: var(--yellow); }
.health-chip.reason { background: #f851490a; color: var(--text-secondary); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Right-aligned action cluster: [Diagnose] [count] [↻] */
.health-actions {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
}

/* Reprobe button — per-component manual refresh */
.health-reprobe-btn {
    font-size: 14px;
    padding: 0;
    width: 26px;
    height: 26px;
    line-height: 26px;
    text-align: center;
    border-radius: 50%;
    border: none;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: color 0.15s, background 0.15s;
    flex-shrink: 0;
}
.health-reprobe-btn:hover { color: var(--text-primary); background: var(--bg-tertiary); }
.health-reprobe-btn.spinning {
    pointer-events: none;
    animation: diagSpin 0.6s linear infinite;
    color: var(--text-secondary);
}

/* Diagnose button — appears on unhealthy components */
.health-diagnose-btn {
    margin-left: auto;
    font-size: 12px;
    padding: 4px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s, border-color 0.15s;
}
.health-diagnose-btn:hover { background: var(--bg-hover, var(--border)); color: var(--text-primary); border-color: var(--text-muted); }
.health-diagnose-btn.loading { pointer-events: none; }

/* Shared spin animation for reprobe ↻ and inline spinners */
@keyframes diagSpin { to { transform: rotate(360deg); } }

.diag-spinner {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid var(--border);
    border-top-color: var(--text-primary);
    border-radius: 50%;
    animation: diagSpin 0.6s linear infinite;
    vertical-align: middle;
}

/* Diagnostic results panel — slides in below the row */
.health-diag {
    border-top: 1px solid var(--border);
    padding: 10px 16px;
    font-size: 12px;
    background: var(--bg-primary);
}
.health-diag-steps {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 8px;
}
.health-diag-step {
    display: flex;
    align-items: baseline;
    gap: 6px;
}
.health-diag-step .step-icon { flex-shrink: 0; }
.health-diag-step .step-desc { color: var(--text-secondary); }
.health-diag-step .step-detail { color: var(--text-muted); font-size: 11px; }
.health-diag-step.fail .step-desc { color: var(--text-primary); }

.health-diag-cause {
    padding: 8px 10px;
    border-radius: 4px;
    background: #f851490a;
    border-left: 3px solid var(--red, #f85149);
    margin-bottom: 6px;
}
.health-diag-cause .cause-label { font-weight: 600; font-size: 11px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 2px; }
.health-diag-cause .cause-text { color: var(--text-primary); }

.health-diag-fix {
    padding: 8px 10px;
    border-radius: 4px;
    background: var(--bg-tertiary);
    border-left: 3px solid var(--blue, #58a6ff);
    white-space: pre-wrap;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-secondary);
    line-height: 1.5;
}
.health-diag-fix .fix-label { font-weight: 600; font-size: 11px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 2px; font-family: var(--font-sans, system-ui); }

/* -- Event log --------------------------------------------------------- */

.log-container {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    max-height: 400px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Code", "Fira Code", monospace;
    font-size: 12px;
    line-height: 1.6;
}

.log-entry {
    padding: 4px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 12px;
}

.log-entry:last-child { border-bottom: none; }
.log-entry:hover { background: var(--bg-tertiary); }

.log-ts {
    color: var(--text-muted);
    white-space: nowrap;
    flex-shrink: 0;
}

.log-kind {
    white-space: nowrap;
    flex-shrink: 0;
    min-width: 110px;
}

.log-msg {
    color: var(--text-primary);
    word-break: break-word;
    flex: 1;
}

.log-actions {
    flex-shrink: 0;
    display: flex;
    align-items: center;
}

.log-entry.info .log-kind { color: var(--text-secondary); }
.log-entry.info .log-ts { color: var(--text-muted); }

.log-entry.warn .log-kind { color: var(--yellow); }
.log-entry.warn .log-msg { color: var(--yellow); }
.log-entry.warn .log-ts { color: var(--yellow); opacity: 0.7; }

.log-entry.error .log-kind { color: var(--red); }
.log-entry.error .log-msg { color: var(--red); }
.log-entry.error .log-ts { color: var(--red); opacity: 0.7; }

/* -- Log toolbar ------------------------------------------------------- */

.log-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
}

.log-toolbar .section-title { margin: 0; padding: 0; border: 0; }

/* -- Task toolbar & scroll --------------------------------------------- */

.task-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
}
.task-toolbar .section-title { margin: 0; padding: 0; border: 0; }
.task-search-input {
    padding: 5px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-tertiary);
    color: var(--text-primary);
    font-size: 12px;
    width: 240px;
}
.task-search-input:focus { outline: none; border-color: var(--accent); }
.task-search-input::placeholder { color: var(--text-muted); }

.task-list-scroll {
    max-height: 500px;
    overflow-y: auto;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
}

.log-toolbar-btn {
    padding: 4px 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-secondary);
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
}

.log-toolbar-btn:hover {
    color: var(--text-primary);
    border-color: var(--text-secondary);
}

.btn-investigate {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s;
}

.btn-investigate.error {
    background: #f851491a;
    border: 1px solid var(--red);
    color: var(--red);
}

.btn-investigate.error:hover {
    background: var(--red);
    color: var(--bg-primary);
}

.btn-investigate.warn {
    background: #d299221a;
    border: 1px solid var(--yellow);
    color: var(--yellow);
}

.btn-investigate.warn:hover {
    background: var(--yellow);
    color: var(--bg-primary);
}

/* -- Workflow tabs ----------------------------------------------------- */

.tab-btn.workflow-tab {
    color: var(--accent);
    border-bottom: 2px solid transparent;
    position: relative;
}
.tab-btn.workflow-tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}

@keyframes tab-flash {
    0%, 100% { border-bottom-color: var(--accent); box-shadow: 0 2px 8px var(--accent-subtle); }
    50% { border-bottom-color: transparent; box-shadow: none; }
}
.tab-btn.flash {
    animation: tab-flash 1.2s ease-in-out infinite;
    border-bottom: 2px solid var(--accent);
}

/* -- Toast notifications ---------------------------------------------- */

.toast-container {
    position: fixed;
    bottom: 24px;
    right: 24px;
    top: 90px;
    z-index: 1000;
    display: flex;
    flex-direction: column-reverse;
    gap: 8px;
    width: 360px;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
    pointer-events: none;
}
.toast { pointer-events: auto; }

@keyframes toast-slide-in {
    from { transform: translateX(120%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}

.toast {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 8px;
    padding: 12px 16px;
    animation: toast-slide-in 0.35s cubic-bezier(0.16, 1, 0.3, 1);
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}

.toast-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
}

.toast-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
}

.toast-close {
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 14px;
    padding: 0 4px;
    line-height: 1;
}
.toast-close:hover { color: var(--text-primary); }

.toast-body {
    font-size: 12px;
    color: var(--text-secondary);
}

.toast-action {
    align-self: flex-end;
    padding: 4px 12px;
    background: var(--accent-subtle);
    border: 1px solid var(--accent);
    border-radius: 4px;
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
}
.toast-action:hover { background: var(--accent); color: var(--bg-primary); }

/* Toast color variants */
.toast.toast-note { border-left-color: var(--note-color); }
.toast.toast-request { border-left-color: var(--request-color); }
.toast.toast-request .toast-action {
    background: var(--request-subtle); border-color: var(--request-color); color: var(--request-color);
}
.toast.toast-request .toast-action:hover { background: var(--request-color); color: var(--bg-primary); }

/* Tab close button */
.tab-close {
    display: inline-flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; margin-left: 6px;
    border: none; background: transparent; color: var(--text-muted);
    font-size: 11px; line-height: 1; cursor: pointer; border-radius: 3px;
    opacity: 0; transition: all 0.15s;
}
.tab-btn:hover .tab-close { opacity: 1; }
.tab-close:hover { background: var(--red); color: #fff; opacity: 1; }

/* Tab color variants */
.tab-btn.workflow-tab.tab-note { color: var(--note-color); }
.tab-btn.workflow-tab.tab-note.active { border-bottom-color: var(--note-color); }
.tab-btn.workflow-tab.tab-request { color: var(--request-color); }
.tab-btn.workflow-tab.tab-request.active { border-bottom-color: var(--request-color); }
@keyframes tab-flash-request {
    0%, 100% { border-bottom-color: var(--request-color); box-shadow: 0 2px 8px var(--request-subtle); }
    50% { border-bottom-color: transparent; box-shadow: none; }
}
.tab-btn.workflow-tab.tab-request.flash {
    animation: tab-flash-request 1.2s ease-in-out infinite;
    border-bottom: 2px solid var(--request-color);
}

/* Type pills */
.type-pill {
    display: inline-flex; align-items: center;
    font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
    text-transform: uppercase; border-radius: 4px;
    padding: 2px 8px; line-height: 1.4; white-space: nowrap;
}
.type-pill.note1 { border: 1px solid var(--note-color); color: var(--note-color); background: transparent; }
.type-pill.request1 { border: 1px solid var(--request-color); color: var(--request-color); background: transparent; animation: pill-pulse 1.5s ease-in-out infinite; }
@keyframes pill-pulse {
    0%, 100% { box-shadow: 0 0 0 0 var(--request-subtle); }
    50% { box-shadow: 0 0 6px 2px var(--request-subtle); }
}
.type-pill.request3 { border: none; background: var(--request-color); color: #1a1a2e; font-weight: 800; }

/* Notification/request buttons */
.nb-btn {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 9px 20px; border-radius: 8px; border: 1px solid;
    background: transparent; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: background 0.15s, border-color 0.15s, color 0.15s;
    white-space: nowrap;
}
.nb-btn-approve { color: var(--btn-approve); border-color: var(--btn-approve); background: var(--btn-approve-subtle); }
.nb-btn-approve:hover { background: var(--btn-approve-mid); }
.nb-btn-approve:active { background: var(--btn-approve); color: var(--bg-primary); }
.nb-btn-deny { color: var(--btn-deny); border-color: var(--btn-deny); background: var(--btn-deny-subtle); }
.nb-btn-deny:hover { background: var(--btn-deny-mid); }
.nb-btn-deny:active { background: var(--btn-deny); color: var(--bg-primary); }
.nb-btn-neutral { color: var(--btn-neutral); border-color: var(--btn-neutral); background: var(--btn-neutral-subtle); }
.nb-btn-neutral:hover { background: var(--btn-neutral-mid); }
.nb-btn-neutral:active { background: var(--btn-neutral); color: var(--bg-primary); }
.nb-btn-request { color: var(--request-color); border-color: var(--request-color); background: var(--request-subtle); }
.nb-btn-request:hover { background: var(--btn-request-mid); }
.nb-btn-request:active { background: var(--request-color); color: var(--bg-primary); }
.nb-btn-ghost { color: var(--text-secondary); border-color: var(--border); background: transparent; }
.nb-btn-ghost:hover { color: var(--text-primary); border-color: var(--text-muted); }
.nb-btn-ghost:active { background: var(--bg-tertiary); }
.nb-btn-group { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.nb-btn-group.stretch .nb-btn { flex: 1; justify-content: center; }

/* -- Triage views — shared -------------------------------------------- */

.wv-header {
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}
.wv-header h2 {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 6px;
    color: var(--text-primary);
}
.wv-header h2 .wv-source-icon { margin-right: 8px; }
.wv-narrative {
    color: var(--text-secondary);
    font-size: 13px;
    line-height: 1.5;
    margin-bottom: 4px;
}
.wv-stats {
    color: var(--text-muted);
    font-size: 12px;
    display: flex;
    gap: 12px;
}
.wv-stats .wv-stat {
    display: flex;
    align-items: center;
    gap: 4px;
}
.wv-stats .wv-stat-num {
    font-weight: 700;
    color: var(--text-primary);
}

/* -- Action color system ---------------------------------------------- */
:root {
    --action-close: #f85149;
    --action-close-bg: #f851490d;
    --action-create: #58a6ff;
    --action-create-bg: #58a6ff0d;
    --action-record: #d2a8ff;
    --action-record-bg: #d2a8ff0d;
    --action-group: #f0883e;
    --action-group-bg: #f0883e0d;
    --action-leave: #3fb950;
    --action-leave-bg: #3fb9500d;
}

/* -- Action pills (replace dropdowns) --------------------------------- */
.wv-action-pills {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
}
.wv-pill {
    padding: 4px 12px;
    border-radius: 16px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-secondary);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
}
.wv-pill:hover { border-color: var(--text-muted); color: var(--text-primary); }
.wv-pill.selected { color: #fff; }
.wv-pill[data-action="close"].selected { background: var(--action-close); border-color: var(--action-close); }
.wv-pill[data-action="create_task"].selected { background: var(--action-create); border-color: var(--action-create); }
.wv-pill[data-action="record_into_task"].selected { background: var(--action-record); border-color: var(--action-record); }
.wv-pill[data-action="group"].selected { background: var(--action-group); border-color: var(--action-group); }
.wv-pill[data-action="leave"].selected { background: var(--action-leave); border-color: var(--action-leave); }

/* -- Section headers -------------------------------------------------- */
.wv-section {
    margin-bottom: 24px;
}
.wv-section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-radius: 8px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: background 0.15s;
}
.wv-section-header:hover { background: var(--bg-tertiary); }
.wv-section-header h3 {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
}
.wv-section-header .wv-section-icon { font-size: 14px; }
.wv-section-header[data-action="close"] h3 { color: var(--action-close); }
.wv-section-header[data-action="create_task"] h3 { color: var(--action-create); }
.wv-section-header[data-action="record_into_task"] h3 { color: var(--action-record); }
.wv-section-header[data-action="group"] h3 { color: var(--action-group); }
.wv-section-header[data-action="leave"] h3 { color: var(--action-leave); }
.wv-section-count {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
}

.wv-confirm-all {
    padding: 4px 12px;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 16px;
    color: var(--text-secondary);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
}
.wv-confirm-all:hover { color: var(--green); border-color: var(--green); }
.wv-confirm-all.confirmed {
    background: var(--green-subtle);
    color: var(--green);
    border-color: var(--green);
    pointer-events: none;
}

/* -- Group cards ------------------------------------------------------ */
/* Card shell */
.wv-group-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 10px;
    transition: border-color 0.2s, box-shadow 0.2s;
}
.wv-group-card:hover { border-color: var(--text-muted); }
.wv-group-card.drag-over {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-subtle), 0 4px 16px rgba(0,0,0,0.2);
}

/* Header: full-width, with border-bottom */
.wv-group-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px 8px;
    border-bottom: 1px solid var(--border);
}
.wv-group-header-left { flex: 1; min-width: 0; }
.wv-group-header-right { flex-shrink: 0; padding-top: 2px; }
.wv-group-intent { font-weight: 700; font-size: 14px; line-height: 1.3; }
.wv-context-subtitle { font-size: 11px; color: var(--accent); margin-top: 2px; line-height: 1.3; }

.wv-badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}
.wv-badge.high { background: #3fb9501a; color: var(--green); }
.wv-badge.medium { background: #d299221a; color: var(--yellow); }
.wv-badge.low { background: #f851491a; color: var(--red); }

/* Body: CSS Grid — 2 columns when task area present, 1 otherwise */
.wv-card-body {
    display: grid;
    grid-template-columns: 1fr;
    grid-template-areas: "main";
}
.wv-card-body.has-task-area {
    grid-template-columns: 1fr 550px;
    grid-template-areas: "main task";
}
.wv-card-main {
    grid-area: main;
    padding: 10px 16px 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
}
.wv-card-task-col {
    grid-area: task;
    border-left: 1px solid var(--border);
    background: var(--bg-tertiary);
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-self: stretch;
}
/* Hide task column when grid is single-column */
.wv-card-body:not(.has-task-area) .wv-card-task-col { display: none; }

.wv-rationale {
    color: var(--text-secondary);
    font-size: 12px;
    line-height: 1.4;
    margin: 0;
}

/* Legacy .wv-context kept for clarify view */
.wv-context {
    font-size: 12px;
    color: var(--text-primary);
    padding: 6px 10px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent);
    margin: 6px 0 10px;
    line-height: 1.4;
}

@media (max-width: 600px) {
    .wv-card-body.has-task-area {
        grid-template-columns: 1fr;
        grid-template-areas: "main" "task";
    }
    .wv-card-task-col { border-left: none; border-top: 1px solid var(--border); }
}

/* -- Draggable items -------------------------------------------------- */
.wv-items-area {
    min-height: 8px;
    border-radius: 6px;
    transition: background 0.15s, min-height 0.15s;
    padding: 2px 0;
}
.wv-items-area.drag-active {
    background: var(--accent-subtle);
    min-height: 40px;
}

.wv-item-toggle {
    font-size: 12px;
    color: var(--accent);
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
    display: inline-block;
    transition: background 0.15s;
}
.wv-item-toggle:hover { background: var(--accent-subtle); }

@keyframes item-appear {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
}

.wv-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    margin: 2px 0;
    border-radius: 6px;
    font-size: 12px;
    background: var(--bg-primary);
    border: 1px solid transparent;
    cursor: grab;
    transition: all 0.15s;
    animation: item-appear 0.2s ease-out;
}
.wv-item:hover {
    border-color: var(--border);
    background: var(--bg-tertiary);
}
.wv-item.dragging {
    opacity: 0.4;
    cursor: grabbing;
}
.wv-item .wv-drag-handle {
    color: var(--text-muted);
    cursor: grab;
    font-size: 10px;
    flex-shrink: 0;
    user-select: none;
}
.wv-item-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.wv-item-label a {
    color: var(--accent);
    text-decoration: none;
}
.wv-item-label a:hover { text-decoration: underline; }

.wv-item-override {
    padding: 2px 6px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 10px;
    flex-shrink: 0;
    cursor: pointer;
    transition: border-color 0.15s;
}
.wv-item-override:hover { border-color: var(--text-muted); }

/* -- New group drop zone ---------------------------------------------- */
.wv-new-group-zone {
    border: 2px dashed var(--border);
    border-radius: 10px;
    padding: 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 12px;
    transition: all 0.2s;
    margin-top: 16px;
}
.wv-new-group-zone.drag-active {
    border-color: var(--accent);
    background: var(--accent-subtle);
    color: var(--accent);
}
.wv-new-group-zone .wv-drop-icon { font-size: 20px; margin-bottom: 4px; }

.wv-new-group-card {
    border-color: var(--accent);
    border-style: dashed;
    background: linear-gradient(135deg, var(--bg-secondary), var(--accent-subtle));
}

/* Override reason (Gap 7) */
.wv-override-reason {
    margin: 4px 0;
}
.wv-override-reason input {
    width: 100%;
    padding: 4px 8px;
    border-radius: 4px;
    border: 1px dashed var(--yellow);
    background: transparent;
    color: var(--text-primary);
    font-size: 11px;
    font-style: italic;
}
.wv-override-reason input:focus { outline: none; border-style: solid; }
.wv-override-reason input::placeholder { color: var(--text-muted); }

/* Empty group dismiss */
.wv-empty-group {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 8px;
    color: var(--text-muted);
    font-size: 11px;
    font-style: italic;
}
.wv-dismiss-btn {
    padding: 3px 10px;
    border-radius: 4px;
    border: 1px solid var(--red);
    background: transparent;
    color: var(--red);
    font-size: 10px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
}
.wv-dismiss-btn:hover { background: #f851491a; }

.wv-new-group-input {
    display: flex;
    gap: 6px;
    align-items: center;
    margin-top: 8px;
    padding: 8px 12px;
    background: var(--bg-secondary);
    border: 1px solid var(--accent);
    border-radius: 8px;
}
.wv-new-group-input input {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 13px;
}
.wv-new-group-input input:focus { outline: none; border-color: var(--accent); }
.wv-new-group-input button {
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--bg-tertiary);
    color: var(--text-primary);
    transition: all 0.15s;
}
.wv-new-group-input button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--bg-primary);
}
.wv-new-group-input button.primary:hover { opacity: 0.9; }

/* -- Clarify view ----------------------------------------------------- */
.wv-question-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 10px;
    transition: border-color 0.2s;
}
.wv-question-card:hover { border-color: var(--text-muted); }

.wv-question-card .wv-group-intent {
    margin-bottom: 8px;
    display: block;
}

.wv-items-context {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0 12px;
}
.wv-context-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 10px;
    border-radius: 14px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-secondary);
    transition: border-color 0.15s;
}
.wv-context-chip a {
    color: var(--accent);
    text-decoration: none;
}
.wv-context-chip a:hover { text-decoration: underline; }

.wv-question {
    margin: 10px 0;
}
.wv-question label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    margin-bottom: 6px;
    color: var(--text-primary);
}
.wv-question input {
    width: 100%;
    padding: 8px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 13px;
    transition: border-color 0.15s;
}
.wv-question input:focus { outline: none; border-color: var(--accent); }
.wv-question input::placeholder { color: var(--text-muted); }

.wv-progress {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
}
.wv-progress-bar {
    flex: 1;
    height: 4px;
    background: var(--bg-tertiary);
    border-radius: 2px;
    overflow: hidden;
}
.wv-progress-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 2px;
    transition: width 0.3s ease;
}
.wv-progress-text {
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
}

/* -- Task search / assignment ----------------------------------------- */

.wv-task-area {
    padding: 0;
}
.wv-task-area-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    color: var(--text-muted);
    margin-bottom: 6px;
}
.wv-task-search-wrap { position: relative; }
.wv-task-search {
    width: 100%;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 12px;
}
.wv-task-search:focus { outline: none; border-color: var(--accent); }
.wv-task-search::placeholder { color: var(--text-muted); }

.wv-task-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    max-height: 200px;
    overflow-y: auto;
    background: var(--bg-secondary);
    border: 1px solid var(--accent);
    border-top: none;
    border-radius: 0 0 6px 6px;
    z-index: 100;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
.wv-task-match {
    padding: 6px 10px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    transition: background 0.1s;
}
.wv-task-match:hover { background: var(--bg-tertiary); }
.wv-task-match-id {
    font-family: monospace;
    font-size: 10px;
    color: var(--text-muted);
    flex-shrink: 0;
}
.wv-task-match-text {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.wv-task-selected {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    background: var(--accent-subtle);
    border: 1px solid var(--accent);
    border-radius: 14px;
    font-size: 11px;
    color: var(--accent);
    max-width: 100%;
    overflow: hidden;
}
.wv-task-selected .wv-task-clear {
    cursor: pointer;
    font-size: 10px;
    opacity: 0.7;
}
.wv-task-selected .wv-task-clear:hover { opacity: 1; }

.wv-new-task-input {
    width: 100%;
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 12px;
}
.wv-new-task-input:focus { outline: none; border-color: var(--accent); }
.wv-task-or {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 6px 0;
}

/* -- Footer ----------------------------------------------------------- */
.wv-footer {
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: flex-end;
    gap: 8px;
}
.wv-submit {
    padding: 10px 24px;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    color: #fff;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.2s;
    box-shadow: 0 2px 8px rgba(216, 120, 87, 0.3);
}
.wv-submit:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(216, 120, 87, 0.4);
}
.wv-submit:active { transform: translateY(0); }
.wv-submit:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
}

/* -- Command Palette --------------------------------------------------- */

.cp-overlay {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 2000;
    background: rgba(0, 0, 0, 0.55);
    justify-content: center;
    padding-top: 15vh;
    align-items: flex-start;
}
.cp-overlay.open { display: flex; }

.cp-modal {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 10px;
    width: 560px;
    height: 480px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
    overflow: hidden;
}

.cp-search-row {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    gap: 10px;
}
.cp-search-icon { color: var(--text-muted); font-size: 16px; flex-shrink: 0; }
.cp-search-input {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: var(--text-primary);
    font-size: 15px;
    font-family: inherit;
}
.cp-search-input::placeholder { color: var(--text-muted); }
.cp-esc-hint {
    color: var(--text-muted);
    font-size: 11px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 6px;
    flex-shrink: 0;
}
.cp-filters {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
}
.cp-filter-pill {
    font-size: 11px;
    border-radius: 4px;
    padding: 2px 8px;
    cursor: pointer;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    font-family: inherit;
    transition: all 0.15s;
}
.cp-filter-pill:hover { border-color: var(--text-muted); }
.cp-filter-pill.active-obsidian {
    color: var(--purple);
    border-color: var(--purple);
    background: var(--purple-subtle);
}
.cp-filter-pill.active-workbuddy {
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-subtle);
}
.cp-filter-pill.active-all {
    color: var(--text-primary);
    border-color: var(--text-muted);
    background: var(--bg-tertiary);
}

.cp-results {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 4px 0;
}
.cp-empty {
    padding: 24px 16px;
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
}
.cp-group-label {
    padding: 8px 16px 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
}
.cp-item {
    display: grid;
    grid-template-columns: auto 1fr auto auto;
    align-items: center;
    padding: 7px 16px;
    cursor: pointer;
    gap: 10px;
    border-left: 3px solid transparent;
}
.cp-type-icon {
    font-size: 13px;
    width: 18px;
    text-align: center;
    flex-shrink: 0;
    opacity: 0.7;
}
.cp-type-icon.cp-type-inline { color: var(--text-muted); }
.cp-type-icon.cp-type-parameterized { color: var(--blue, #5b9bd5); }
.cp-type-icon.cp-type-workflow { color: var(--orange, #e5a045); }
.cp-item:hover, .cp-item.active {
    background: var(--bg-tertiary);
}
.cp-item.active { border-left-color: var(--accent); }
.cp-item-name {
    color: var(--text-primary);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.cp-item-desc {
    color: var(--text-muted);
    font-size: 11px;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.cp-item-provider {
    font-size: 10px;
    border-radius: 3px;
    padding: 1px 5px;
    flex-shrink: 0;
}
.cp-item-provider.cp-prov-obsidian {
    color: var(--purple);
    background: var(--purple-subtle);
}
.cp-item-provider.cp-prov-workbuddy {
    color: var(--accent);
    background: var(--accent-subtle);
}
.cp-status-bar {
    padding: 6px 16px;
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-muted);
    display: flex;
    justify-content: space-between;
}

/* Param form inside the palette modal */
.cp-param-form { display: none; padding: 12px 16px; }
.cp-param-form.open { display: block; flex: 1; min-height: 0; overflow-y: auto; }
.cp-param-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 12px;
}
.cp-param-field { margin-bottom: 10px; }
.cp-param-field label {
    display: block;
    font-size: 12px;
    color: var(--text-secondary);
    margin-bottom: 4px;
}
.cp-param-field input, .cp-param-field textarea {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 13px;
    padding: 6px 10px;
    outline: none;
}
.cp-param-field input:focus, .cp-param-field textarea:focus {
    border-color: var(--accent);
}
.cp-param-field .cp-param-hint {
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 2px;
}
.cp-param-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 12px;
}
.cp-param-actions button {
    padding: 6px 16px;
    border-radius: 5px;
    border: 1px solid var(--border);
    cursor: pointer;
    font-size: 13px;
    font-family: inherit;
}
.cp-btn-run {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent) !important;
}
.cp-btn-run:hover { opacity: 0.9; }
.cp-btn-cancel {
    background: transparent;
    color: var(--text-secondary);
}
.cp-btn-cancel:hover { background: var(--bg-tertiary); }

.cp-kbd-hint {
    font-size: 11px;
    color: var(--text-muted);
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    margin-left: 8px;
    cursor: pointer;
}
.cp-kbd-hint:hover { color: var(--text-secondary); }

/* -- Responsive -------------------------------------------------------- */

/* ── Thread chat component ───────────────────────────────── */
/* Mountable chat UI. Lives inside tab panels (or any flex container).
   For standalone thread views, fills the panel. For split layouts,
   sits alongside other content via .thread-split-layout.              */

/* Split layout: content left, chat right. Any view can use this. */
.thread-split-layout {
    display: flex; gap: 0;
    height: calc(100vh - 120px);
}
.thread-split-layout > .thread-split-content {
    flex: 1; overflow-y: auto; padding: 24px;
    min-width: 0; /* prevent flex blowout */
}
.thread-split-layout > .thread-chat-pane {
    width: 360px; flex-shrink: 0;
    border-left: 1px solid var(--border);
    display: flex; flex-direction: column;
}

/* Standalone chat (fills entire tab panel) */
.thread-chat-standalone {
    display: flex; flex-direction: column;
    height: calc(100vh - 120px);
    max-width: 640px; margin: 0 auto;
}

/* Shared chat internals — used in both standalone and pane modes */
.thread-chat-messages {
    flex: 1; overflow-y: auto; padding: 12px 14px;
    display: flex; flex-direction: column; gap: 0;
}

/* Thread-msg classes removed — threads now use shared .chat-msg styles.
   See "Chats tab" section below for .chat-msg, .chat-msg-bubble, .chat-msg-meta. */

.thread-input {
    display: flex; gap: 6px; padding: 10px 14px;
    border-top: 1px solid var(--border); flex-shrink: 0;
}
.thread-input input {
    flex: 1; padding: 7px 10px;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg-secondary); color: var(--text-primary);
    font-size: 13px; outline: none;
}
.thread-input input:focus { border-color: var(--accent); }
.thread-input button {
    padding: 7px 14px; border-radius: 8px;
    background: var(--accent); color: #fff; border: none;
    font-size: 13px; cursor: pointer; white-space: nowrap;
}
.thread-input button:hover { opacity: .85; }

.thread-status-bar {
    padding: 5px 14px; text-align: center;
    font-size: 11px; color: var(--text-muted);
    border-top: 1px solid var(--border); flex-shrink: 0;
}

@media (max-width: 768px) {
    .card-grid { grid-template-columns: 1fr; }
    .tab-panel { padding: 16px; }
    .data-table { font-size: 12px; }
    .toast-container { left: 16px; right: 16px; max-width: none; }
    .cp-modal { width: 95vw; }
    .thread-split-layout { flex-direction: column; }
    .thread-split-layout > .thread-chat-pane { width: 100%; border-left: none; border-top: 1px solid var(--border); }
    .chats-layout { flex-direction: column; }
    .chats-list-panel { flex: none !important; max-height: 250px; }
}

/* -- Chats tab --------------------------------------------------------- */

.chats-search-bar { display: flex; gap: 8px; margin-bottom: 16px; }
.chats-search-input {
    flex: 1; padding: 10px 14px;
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text-primary);
    font-size: 14px; font-family: inherit;
}
.chats-search-input:focus { outline: none; border-color: var(--accent); }
.chats-select {
    padding: 8px 10px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text-secondary); font-size: 12px;
}
.chats-project-select.active {
    border-color: var(--accent); color: var(--accent);
}
.chats-accent-btn {
    padding: 8px 18px; background: var(--accent); color: #fff;
    border: none; border-radius: 6px; font-size: 13px;
    font-weight: 500; cursor: pointer;
}
.chats-accent-btn:hover { opacity: 0.9; }
.chats-search-results {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 16px;
    max-height: 400px; overflow-y: auto;
}
.chats-search-hit {
    padding: 10px 12px; border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background 0.15s;
}
.chats-search-hit:hover { background: var(--bg-tertiary); }
.chats-search-hit:last-child { border-bottom: none; }
.chats-hit-score { font-size: 11px; color: var(--accent); font-weight: 600; }
.chats-hit-session { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
.chats-hit-text {
    font-size: 13px; color: var(--text-secondary); margin-top: 4px;
    display: -webkit-box; -webkit-line-clamp: 3;
    -webkit-box-orient: vertical; overflow: hidden;
}
.chats-layout { display: flex; gap: 16px; min-height: 600px; }
.chats-list-panel { flex: 0 0 340px; overflow-y: auto; max-height: 80vh; }
.chats-viewer-panel { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.chats-list-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.chat-card {
    padding: 12px; margin-bottom: 4px; border-radius: 6px;
    cursor: pointer; border: 1px solid var(--border);
    background: var(--bg-secondary);
    transition: background 0.15s, border-color 0.15s;
}
.chat-card:hover { background: var(--bg-tertiary); }
.chat-card.active { background: var(--bg-tertiary); border-color: var(--accent); }
.chat-card-title {
    font-size: 13px; font-weight: 500; color: var(--text-primary);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chat-card-meta {
    display: flex; gap: 12px; font-size: 11px;
    color: var(--text-muted); margin-top: 4px;
}
.chat-card-project {
    font-size: 10px; color: var(--accent); font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px;
}
.chat-card-tools { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.chats-viewer-header {
    padding: 12px 16px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-radius: 8px 8px 0 0;
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0;
}
.chats-hdr-left { font-size: 13px; color: var(--text-secondary); }
.chats-hdr-left code { color: var(--text-primary); }
.chats-hdr-right { display: flex; gap: 6px; flex-wrap: wrap; }
.chats-hdr-btn {
    padding: 4px 10px; background: var(--bg-tertiary);
    border: 1px solid var(--border); border-radius: 4px;
    color: var(--text-secondary); font-size: 12px; cursor: pointer;
}
.chats-hdr-btn:hover { color: var(--text-primary); border-color: var(--accent); }
.chats-hdr-btn.active { border-color: var(--accent); color: var(--accent); }
.chats-in-search {
    padding: 8px 12px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-top: none;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
.chats-in-search input {
    flex: 1; min-width: 150px; padding: 6px 10px;
    background: var(--bg-tertiary); border: 1px solid var(--border);
    border-radius: 4px; color: var(--text-primary); font-size: 13px;
    font-family: inherit;
}
.chats-in-search input:focus { outline: none; border-color: var(--accent); }
.chats-in-search button {
    padding: 4px 12px; background: var(--bg-tertiary);
    border: 1px solid var(--border); border-radius: 4px;
    color: var(--text-secondary); font-size: 12px; cursor: pointer;
}
.chats-in-search button:hover { color: var(--text-primary); }
.chats-messages {
    border: 1px solid var(--border); border-top: none;
    border-radius: 0 0 8px 8px; padding: 16px;
    flex: 1; overflow-y: auto; background: var(--bg-primary);
    min-height: 300px; max-height: 65vh;
}
.chat-msg { margin-bottom: 12px; display: flex; flex-direction: column; }
.chat-msg.user { align-items: flex-end; }
.chat-msg.assistant { align-items: flex-start; }
.chat-msg-bubble {
    max-width: 85%; padding: 10px 14px; border-radius: 12px;
    font-size: 13px; line-height: 1.5; cursor: pointer;
    position: relative; word-wrap: break-word; overflow-wrap: break-word;
}
.chat-msg.user .chat-msg-bubble {
    background: var(--accent-subtle); border: 1px solid #D8785755;
    border-radius: 12px 12px 2px 12px;
}
.chat-msg.assistant .chat-msg-bubble {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 12px 12px 12px 2px;
}
.chat-msg-bubble.expanded {
    max-width: 100%; white-space: pre-wrap;
    font-family: var(--font-mono); font-size: 12px;
}
.chat-msg-bubble.in-span { box-shadow: 0 0 0 2px var(--accent); }
.chat-msg-meta {
    font-size: 10px; color: var(--text-muted);
    margin-top: 2px; padding: 0 4px;
    display: flex; align-items: center; gap: 6px;
}
.chat-msg-tools { display: flex; gap: 3px; flex-wrap: wrap; }
.chat-msg-tool-badge {
    font-size: 10px; padding: 1px 5px; border-radius: 3px;
    background: #bc8cff22; color: #bc8cff;
}
/* System messages (used by thread chat) */
.chat-msg.system { align-items: center; }
.chat-msg.system .chat-msg-bubble {
    background: transparent; border: none;
    font-size: 11px; color: var(--text-muted);
    font-style: italic; padding: 4px 8px;
    cursor: default; max-width: 100%;
}
/* Choice buttons inside bubbles (used by thread chat) */
.chat-msg-bubble .msg-choices {
    display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px;
}
.chat-msg-bubble .msg-choices button {
    padding: 4px 12px; border-radius: 6px; font-size: 12px;
    border: 1px solid var(--border); background: var(--bg-tertiary);
    color: var(--text-primary); cursor: pointer; transition: all .15s;
}
.chat-msg-bubble .msg-choices button:hover {
    background: var(--accent); color: #fff; border-color: var(--accent);
}
/* Thread chat: bubbles are not clickable (no expand/collapse) */
.thread-chat-messages .chat-msg-bubble { cursor: default; }
.chat-commit-marker {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px; margin: 8px 0;
    background: #3fb95015; border: 1px solid #3fb95033;
    border-radius: 6px; font-size: 12px; color: #3fb950;
    min-width: 0;
}
.chat-commit-marker.clickable { cursor: pointer; }
.chat-commit-marker.clickable:hover { background: #3fb95025; border-color: #3fb95066; }
.chat-commit-marker code { color: var(--text-primary); font-size: 11px; flex-shrink: 0; }
.chat-commit-marker .commit-msg {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    min-width: 0; flex: 1 1 auto;
}
.chat-commit-marker .commit-meta {
    display: flex; align-items: center; gap: 8px;
    flex-shrink: 0; white-space: nowrap;
    color: var(--text-muted); font-size: 11px;
}
.chats-load-more-btn {
    display: block; width: 100%; padding: 8px;
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text-secondary);
    font-size: 12px; cursor: pointer; text-align: center; margin-bottom: 8px;
}
.chats-load-more-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
#chats-commits-bar {
    padding: 8px 12px; background: var(--bg-secondary);
    border: 1px solid var(--border); border-top: none;
    max-height: 120px; overflow-y: auto;
}

/* Grouped search results */
.chats-search-session-group {
    border: 1px solid var(--border); border-radius: 6px;
    margin-bottom: 8px; overflow: hidden;
}
.chats-search-session-hdr {
    padding: 10px 12px; background: var(--bg-secondary);
    cursor: pointer; transition: background 0.15s;
}
.chats-search-session-hdr:hover { background: var(--bg-tertiary); }
.chats-search-chunk {
    padding: 6px 12px 6px 28px;
    border-top: 1px solid var(--border);
    cursor: pointer; transition: background 0.15s;
    background: var(--bg-primary);
}
.chats-search-chunk:hover { background: var(--bg-tertiary); }
"""


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
