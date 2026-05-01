"""Dashboard HTML structure."""

from __future__ import annotations


def _html() -> str:
    return """
<header class="header">
    <h1><span>work-buddy</span> dashboard</h1>
    <div class="header-meta">
        <span id="sidecar-status"><span class="status-dot stopped"></span> loading...</span>
        <span id="event-bus-status" title="Real-time event stream"
              class="bus-status connecting">
            <span class="status-dot stopped"></span> live
        </span>
        <span id="clock"></span>
        <span class="cp-kbd-hint" onclick="cpOpen()" title="Command palette">Ctrl+K</span>
        <button class="header-settings-btn" onclick="switchTab('settings')"
                title="Settings &mdash; component preferences &amp; control graph"
                aria-label="Open Settings">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
                 width="18" height="18" fill="none"
                 stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round"
                 aria-hidden="true">
                <circle cx="12" cy="12" r="3"></circle>
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83
                         2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33
                         1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2
                         v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33
                         l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83
                         l.06-.06a1.65 1.65 0 0 0 .33-1.82
                         1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2
                         2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9
                         a1.65 1.65 0 0 0-.33-1.82l-.06-.06
                         a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06
                         a1.65 1.65 0 0 0 1.82.33H9
                         a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2
                         2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51
                         1.65 1.65 0 0 0 1.82-.33l.06-.06
                         a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06
                         a1.65 1.65 0 0 0-.33 1.82V9
                         a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2
                         2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
            </svg>
        </button>
    </div>
</header>

<nav class="tab-bar">
    <div class="tab-bar-left">
        <button class="tab-btn active" data-tab="overview">Overview</button>
        <button class="tab-btn" data-tab="tasks">Tasks</button>
        <button class="tab-btn" data-tab="review">Review</button>
        <button class="tab-btn" data-tab="review-queue"
                title="Tier-3 outputs awaiting accept/revise/reject">Review Queue</button>
        <button class="tab-btn" data-tab="daily-log"
                title="Tier-4 autonomous actions, collapsible by category">Daily Log</button>
        <button class="tab-btn" data-tab="status">Status</button>
        <button class="tab-btn" data-tab="chats">Chats</button>
        <button class="tab-btn" data-tab="contracts">Contracts</button>
        <button class="tab-btn" data-tab="projects">Projects</button>
        <button class="tab-btn" data-tab="costs">Costs</button>
        <!-- Settings is an off-nav tab reached via the gear icon in the
             header. The panel still lives below (#panel-settings) and
             is still registered in staticLoaders, but it's not part of
             the primary navigation rhythm. -->
        <button class="tab-btn" data-tab="settings" style="display:none" aria-hidden="true"></button>
    </div>
    <div class="tab-bar-right" id="workflow-tabs"></div>
</nav>

<!-- ============================================================ -->
<!-- TAB PANELS — add new tabs by duplicating this pattern         -->
<!-- ============================================================ -->

<!-- OVERVIEW -->
<div class="tab-panel active" id="panel-overview">
    <div class="card-grid" id="overview-cards">
        <div class="loading">Loading system state...</div>
    </div>
    <div class="section-title">Scheduled Jobs</div>
    <div id="overview-jobs"><div class="loading">Loading...</div></div>

</div>

<!-- TASKS -->
<div class="tab-panel" id="panel-tasks">
    <div class="card-grid" id="task-counts"></div>
    <div class="task-toolbar">
        <div class="section-title"><a id="master-task-link" href="#" style="color: var(--accent); text-decoration: none;" title="Open in Obsidian">Master Task List</a> <span id="task-namespace-breadcrumb" class="task-namespace-breadcrumb"></span></div>
        <div class="task-toolbar-controls">
            <div id="task-state-chips" class="task-state-chips" title="Toggle which task states to show"></div>
            <input type="text" id="task-search" class="task-search-input" placeholder="Filter tasks..." />
        </div>
    </div>
    <div class="task-layout">
        <aside id="task-namespace-tree" class="task-namespace-tree">
            <div class="loading">Loading namespaces...</div>
        </aside>
        <div id="task-list" class="task-list-col"><div class="loading">Loading tasks...</div></div>
    </div>
</div>

<!-- REVIEW -->
<!-- Background-triage pending-review pool. Populated by the hourly
     journal-triage cron (and any future background-triage producer).
     Inline, persistent view — uses the shared triage renderer from
     script_triage.py (same layout as the Chrome triage modal). -->
<div class="tab-panel" id="panel-review">
    <div class="review-toolbar">
        <div class="section-title">Triage Review</div>
        <select id="review-source-filter" class="chats-select" onchange="loadReview()">
            <option value="">All sources</option>
            <option value="journal_thread">Journal</option>
            <option value="chrome_tab">Chrome</option>
            <option value="inline">Inline</option>
        </select>
        <button class="chats-accent-btn" onclick="loadReview()">Refresh</button>
    </div>
    <div id="review-narrative" class="review-narrative"></div>
    <div id="review-groups"><div class="loading">Loading review items...</div></div>
</div>

<!-- REVIEW QUEUE (Slice 4) — tier-3 outputs awaiting review.
     Populated by /api/automation/review-queue. Each card is rendered
     via the Slice 1.5 Resolution Surface primitives so the keyboard
     layer (j/k/enter/r/s/?) and Defer / Re-direct affordances stay
     consistent with the main Review tab. The two surfaces serve
     different content shapes (pool entries vs task_metadata rows)
     but share the UX language. -->
<div class="tab-panel" id="panel-review-queue">
    <div class="review-toolbar">
        <div class="section-title">Review Queue
            <span class="section-subtitle"
                  title="Tier-3 outputs awaiting accept / revise / reject">
                tier-3 surface · Slice 4
            </span>
        </div>
        <button class="chats-accent-btn" onclick="loadReviewQueue()">Refresh</button>
    </div>
    <div id="review-queue-summary" class="review-narrative"></div>
    <div id="review-queue-items"><div class="loading">Loading review queue...</div></div>
</div>

<!-- DAILY LOG (Slice 4) — tier-4 autonomous actions, collapsible by
     category. Read-only in Slice 4; demote-category lands in a
     follow-up. Reads /api/automation/daily-log. -->
<div class="tab-panel" id="panel-daily-log">
    <div class="review-toolbar">
        <div class="section-title">Daily Log
            <span class="section-subtitle"
                  title="Tier-4 actions taken autonomously, grouped by category">
                tier-4 surface · Slice 4
            </span>
        </div>
        <select id="daily-log-window" class="chats-select"
                onchange="loadDailyLog()" title="Look-back window">
            <option value="1" selected>1 day</option>
            <option value="3">3 days</option>
            <option value="7">7 days</option>
        </select>
        <button class="chats-accent-btn" onclick="loadDailyLog()">Refresh</button>
    </div>
    <div id="daily-log-summary" class="review-narrative"></div>
    <div id="daily-log-categories"><div class="loading">Loading daily log...</div></div>
</div>

<!-- Item-detail drawer. Persistent in the DOM, slides in/out from
     the right. Populated by script_review.py when an item's eye
     icon is clicked. Lives outside the tab panels so it overlays
     the whole page regardless of which tab is active. -->
<aside id="review-drawer" class="review-drawer" aria-hidden="true">
    <div class="review-drawer-header">
        <div class="review-drawer-title" id="review-drawer-title">Item detail</div>
        <button class="review-drawer-close" type="button"
                onclick="closeReviewDrawer()" title="Close">&times;</button>
    </div>
    <div class="review-drawer-body" id="review-drawer-body"></div>
</aside>

<!-- STATUS -->
<div class="tab-panel" id="panel-status">
    <div id="status-bridge"><div class="loading">Loading bridge status...</div></div>
    <div class="section-title" style="margin-top: 24px;">Components</div>
    <div id="status-services"><div class="loading">Loading health...</div></div>
    <div class="log-toolbar" style="margin-top: 24px;">
        <span class="section-title">Event Log</span>
        <button class="log-toolbar-btn" onclick="copyLog()" title="Copy log to clipboard">Copy Log</button>
    </div>
    <div id="status-log"><div class="loading">Loading events...</div></div>
    <div class="section-title" style="margin-top: 24px;">Recent Notifications</div>
    <div id="status-notif-log"><div class="empty-state">No notifications yet</div></div>
</div>

<!-- CHATS -->
<div class="tab-panel" id="panel-chats">
    <!-- Global search bar -->
    <div class="chats-search-bar">
        <input type="text" id="chats-global-search" class="chats-search-input"
               placeholder="Search across all chats..." />
        <select id="chats-project-filter" class="chats-select chats-project-select"
                onchange="chatsProjectFilterChanged(this.value)">
            <option value="">All repos</option>
        </select>
        <select id="chats-search-method" class="chats-select" onchange="chatsSearchMethodChanged(this.value)">
            <option value="keyword,semantic">Hybrid</option>
            <option value="keyword">Keyword</option>
            <option value="semantic">Semantic</option>
            <option value="substring">Exact match</option>
        </select>
        <button class="chats-accent-btn" onclick="chatsGlobalSearch()">Search</button>
    </div>

    <!-- Search results (hidden by default) -->
    <div id="chats-search-results" class="chats-search-results" style="display:none;"></div>

    <!-- Two-panel layout -->
    <div class="chats-layout">
        <!-- Left panel: chat list -->
        <div class="chats-list-panel">
            <div class="chats-list-toolbar">
                <select id="chats-sort" class="chats-select">
                    <option value="recent">Most Recent</option>
                    <option value="longest">Longest Duration</option>
                    <option value="most-messages">Most Messages</option>
                </select>
                <select id="chats-days" class="chats-select">
                    <option value="7">7 days</option>
                    <option value="14" selected>14 days</option>
                    <option value="30">30 days</option>
                    <option value="60">60 days</option>
                </select>
            </div>
            <div id="chats-list"><div class="loading">Loading chats...</div></div>
        </div>

        <!-- Right panel: chat viewer -->
        <div class="chats-viewer-panel">
            <div class="chats-viewer-empty" id="chats-viewer-empty">
                <div class="empty-state" style="margin-top:80px;">Select a chat to view the conversation</div>
            </div>
            <div id="chats-viewer" style="display:none;">
                <div class="chats-viewer-header" id="chats-viewer-header"></div>
                <!-- In-chat search (toggleable) -->
                <div class="chats-in-search" id="chats-in-search" style="display:none;">
                    <input type="text" id="chats-in-search-input" placeholder="Search in this chat..." />
                    <button onclick="chatsInSessionSearch()">Find</button>
                    <button onclick="chatsCloseInSearch()">Close</button>
                    <div id="chats-in-search-hits" style="display:flex;gap:4px;flex-wrap:wrap;"></div>
                </div>
                <!-- Commits bar -->
                <div id="chats-commits-bar" style="display:none;"></div>
                <!-- Message list -->
                <div class="chats-messages" id="chats-messages">
                    <div id="chats-load-earlier" style="display:none;">
                        <button class="chats-load-more-btn" onclick="chatsLoadEarlier()">Load earlier messages</button>
                    </div>
                    <div id="chats-message-list"></div>
                    <div id="chats-load-later" style="display:none;">
                        <button class="chats-load-more-btn" onclick="chatsLoadLater()">Load more messages</button>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- CONTRACTS -->
<div class="tab-panel" id="panel-contracts">
    <div id="contracts-table"><div class="loading">Loading contracts...</div></div>
</div>

<!-- COSTS -->
<!-- LLM cost / usage view. Two complementary data sources behind the
     scenes — work-buddy's per-call log + Claude Code session transcripts.
     The user picks a project; the UI decides which data is relevant. -->
<div class="tab-panel" id="panel-costs">
    <div class="costs-toolbar">
        <div class="costs-toolbar-left">
            <select id="costs-project" class="chats-select chats-project-select"
                    onchange="costsProjectChanged(this.value)">
                <option value="">All projects</option>
            </select>
            <select id="costs-range" class="chats-select" onchange="costsRangeChanged(this.value)">
                <option value="today">Today</option>
                <option value="7">Last 7 days</option>
                <option value="30" selected>Last 30 days</option>
                <option value="90">Last 90 days</option>
                <option value="all">All time</option>
            </select>
        </div>
        <div class="costs-toolbar-right">
            <!-- Rate-limit headroom chip + popover. Hidden until we have
                 at least one observation. Click expands; click outside
                 collapses. See script_costs.py for behaviour. -->
            <span id="costs-rate-chip" class="costs-rate-chip"
                  style="display: none;"
                  onclick="costsToggleRateLimitPopover(event)"
                  title="Click for per-model breakdown">
                Limits: <span id="costs-rate-pct">—</span>
            </span>
            <div id="costs-rate-popover" class="costs-rate-popover" style="display: none;"></div>
            <span id="costs-meta" class="costs-meta"></span>
            <button class="chats-accent-btn" onclick="loadCosts(true)">Refresh</button>
        </div>
    </div>

    <!-- Activity pill bar: visible only when project = work-buddy. "Programmatic"
         is the umbrella for everything work-buddy's runner started (cloud + local).
         API and Local drill into the cloud / local backends individually. -->
    <div id="costs-activity-row" class="costs-activity-row" style="display:none;">
        <span class="costs-filter-label">Activity:</span>
        <div class="costs-activity-pills" id="costs-activity-pills">
            <button class="costs-pill active" data-activity="all"
                    onclick="costsActivityChanged('all')">All</button>
            <button class="costs-pill" data-activity="claude_code"
                    onclick="costsActivityChanged('claude_code')">Claude Code</button>
            <button class="costs-pill" data-activity="programmatic"
                    onclick="costsActivityChanged('programmatic')"
                    title="work-buddy's runner activity \u2014 API + Local combined">Programmatic</button>
            <button class="costs-pill" data-activity="api"
                    onclick="costsActivityChanged('api')">API</button>
            <button class="costs-pill" data-activity="local"
                    onclick="costsActivityChanged('local')">Local</button>
        </div>
    </div>

    <div id="costs-models-filter" class="costs-models-filter"></div>

    <div class="card-grid" id="costs-cards">
        <div class="loading">Loading costs...</div>
    </div>

    <div class="costs-charts-row">
        <div class="costs-chart-card">
            <div class="section-title">Daily token volume</div>
            <div class="costs-chart-wrap"><canvas id="costs-daily-chart"></canvas></div>
        </div>
        <div class="costs-chart-card">
            <div class="section-title" id="costs-model-chart-title">Cost by model</div>
            <div class="costs-chart-wrap"><canvas id="costs-model-chart"></canvas></div>
        </div>
    </div>

    <div class="costs-charts-row">
        <div class="costs-chart-card">
            <div class="section-title" id="costs-task-title">Top callers (by cost)</div>
            <div class="costs-chart-wrap"><canvas id="costs-task-chart"></canvas></div>
        </div>
        <div class="costs-chart-card">
            <div class="section-title" id="costs-mode-title">Cloud vs Local mix</div>
            <div class="costs-chart-wrap"><canvas id="costs-mode-chart"></canvas></div>
        </div>
    </div>

    <div class="section-title">Cost by model</div>
    <div id="costs-model-table"></div>

    <div class="costs-sessions-header">
        <div class="section-title" style="margin:0;">Sessions</div>
        <span id="costs-sessions-count" class="costs-meta"></span>
    </div>
    <div id="costs-sessions-table"></div>
    <div id="costs-sessions-pager" class="costs-pager"></div>

    <div class="costs-footer-note">
        Cost estimates use Anthropic published rates (April 2026). Local model
        calls log $0.00 by design.
    </div>
</div>

<!-- SETTINGS -->
<!-- Unified control-graph view: domains → subsystems → components →
     requirements + affected capabilities. Read-only in Phase E;
     preference toggles land in Phase F. -->
<div class="tab-panel" id="panel-settings">
    <div class="settings-toolbar">
        <div class="section-title">Control Graph</div>
        <div class="settings-toolbar-controls">
            <input type="text" id="settings-filter" class="task-search-input" placeholder="Filter by label or id (matches cascade up through parents)" />
            <button class="chats-accent-btn" onclick="reprobeAll(this)"
                    title="Re-run every tool probe from scratch, then rebuild the graph. Takes up to ~10s if Obsidian or another service is slow. Use when the tree shows 'unknown' badges and you want definitive state right now.">Reprobe all</button>
        </div>
    </div>
    <div id="settings-summary" class="settings-summary"></div>
    <div id="settings-tree"><div class="loading">Loading control graph...</div></div>
</div>

<!-- PROJECTS -->
<div class="tab-panel" id="panel-projects">
    <div style="display:flex; gap:24px; min-height:500px;">
        <div id="projects-list" style="flex:0 0 340px; overflow-y:auto; max-height:80vh;">
            <div class="loading">Loading projects...</div>
        </div>
        <div id="project-detail" style="flex:1; min-width:0;">
            <div class="empty-state" style="margin-top:80px;">Select a project to view details</div>
        </div>
    </div>
</div>

<!-- Command Palette -->
<div class="cp-overlay" id="cp-overlay" onclick="if(event.target===this)cpClose()">
    <div class="cp-modal">
        <div class="cp-search-row">
            <span class="cp-search-icon">&#128269;</span>
            <input class="cp-search-input" id="cp-input" type="text"
                   placeholder="Type a command..." autocomplete="off" />
            <div class="cp-filters">
                <button class="cp-filter-pill active-all" data-cp-filter="all">All</button>
                <button class="cp-filter-pill" data-cp-filter="obsidian">obsidian</button>
                <button class="cp-filter-pill" data-cp-filter="work-buddy">work-buddy</button>
            </div>
            <span class="cp-esc-hint">esc</span>
        </div>
        <div class="cp-results" id="cp-results">
            <div class="cp-empty">Press Ctrl+K to search commands</div>
        </div>
        <div class="cp-param-form" id="cp-param-form"></div>
        <div class="cp-status-bar" id="cp-status-bar">
            <span id="cp-status-left">Loading...</span>
            <span id="cp-status-right">&uarr;&darr; navigate &middot; &crarr; run &middot; esc close</span>
        </div>
    </div>
</div>

<!-- Toast notification container -->
<div class="toast-container" id="toast-container"></div>
"""


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------
