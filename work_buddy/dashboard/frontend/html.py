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
        <button class="tab-btn" data-tab="threads"
                title="Threads — unified resolution surface">Threads</button>
        <button class="tab-btn" data-tab="today"
                title="What should I do right now? /wb-task-me view.">Today</button>
        <button class="tab-btn" data-tab="tasks">Tasks</button>
        <button class="tab-btn" data-tab="status">Status</button>
        <button class="tab-btn" data-tab="jobs"
                title="Scheduled jobs — user jobs above, system jobs collapsed.">Jobs</button>
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
</div>

<!-- THREADS — Unified resolution surface. The panel is rendered
     by window.loadThreads (in the threads frontend module). -->
<div class="tab-panel" id="panel-threads">
    <div class="loading">Loading Threads...</div>
</div>

<!-- TODAY — re-runnable "what should I do right now?" view.
     Reads /api/automation/today. Composes the engage view with the
     clamp-to-now plan + a top-1-2 recommendation card. Re-run by
     clicking refresh; persistent context preset shared with the
     Engage tab via localStorage. -->
<div class="tab-panel" id="panel-today">
    <div class="review-toolbar">
        <div class="section-title">Today
            <span class="section-subtitle"
                  title="Re-runnable engage view backed by /wb-task-me.">
                engage
            </span>
        </div>
        <select id="today-context-preset" class="chats-select"
                onchange="onTodayContextPresetChange()"
                title="Your current context — feeds the recommender">
            <option value="at_desk">At desk + online</option>
            <option value="phone_only">Phone only</option>
            <option value="untethered">Untethered</option>
            <option value="custom">Custom…</option>
        </select>
        <button class="chats-accent-btn" onclick="loadToday()">Re-run</button>
    </div>
    <div id="today-now-banner" class="today-now-banner"></div>
    <div id="today-contracts-banner" class="today-contracts-banner"></div>
    <div id="today-recommendations"
         class="today-recommendations"></div>
    <div class="section-title" style="margin-top:14px">Time-blocked plan
        <span class="section-subtitle">clamp-to-now · day_planner</span>
    </div>
    <div id="today-plan"><div class="loading">Loading plan...</div></div>
</div>

<!-- TASKS -->
<div class="tab-panel" id="panel-tasks">
    <div class="task-toolbar">
        <div class="section-title"><a id="master-task-link" href="#" style="color: var(--accent); text-decoration: none;" title="Open in Obsidian">Master Task List</a> <span id="task-namespace-breadcrumb" class="task-namespace-breadcrumb"></span></div>
        <div class="task-toolbar-controls">
            <div id="task-state-chips" class="task-state-chips" title="Toggle which task states to show"></div>
            <span id="task-filter-status" class="task-filter-status" title="Filtered task count and last sync time"></span>
            <button id="task-sync-btn" class="task-sync-btn" title="Run task_sync now (refreshes the view when done)">↻ Sync</button>
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

<!-- (Removed: Review tab + drawer — retired in clarify -> Threads
     migration. Triage now flows through the unified source pipeline
     and surfaces on the Threads tab via group sub-threads.) -->

<!-- Reusable chat sidebar. Slides in from the right when
     window.wbChatSidebar.open(...) is called; mounts the existing
     conversation_chat renderer (attachConversationChat) into the body.
     The sidebar squishes the main content via body padding-right (see
     core/chat_sidebar.py styles). Persistent in the DOM, hidden by
     default via transform: translateX(100%). -->
<aside id="wb-chat-sidebar" class="wb-chat-sidebar" aria-hidden="true">
    <div class="wb-chat-header">
        <div class="wb-chat-title" id="wb-chat-title"></div>
        <button class="wb-chat-close" type="button" aria-label="Close chat"
                onclick="window.wbChatSidebar.close()">&times;</button>
    </div>
    <div class="wb-chat-body" id="wb-chat-body"></div>
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

<!-- JOBS — scheduled cron jobs split by source. User-authored jobs are
     primary; system jobs (shipping with work-buddy) are tucked under a
     <details> disclosure. The Add-job form posts to /api/user_jobs and
     drops a .md file under <data_root>/user_jobs/; the scheduler hot-
     reloads (~30s) and the table refreshes on submit. -->
<div class="tab-panel" id="panel-jobs">
    <div class="jobs-toolbar">
        <div class="section-title" style="margin: 0;">Your Jobs</div>
        <button id="jobs-add-btn" class="jobs-add-btn" type="button"
                onclick="showAddJobForm()">+ Add job</button>
    </div>

    <div id="jobs-add-form" class="jobs-add-form" hidden>
        <!-- Chat-walkthrough escape hatch — for users who don't want to fill
             this form by hand. Opens the chat sidebar with bound_tab='jobs';
             the agent populates these same fields live as it gathers info,
             then submits via user_job_create when the user confirms. -->
        <button id="jobs-help-btn" class="jobs-form-help-btn" type="button"
                onclick="onJobsHelpClick()"
                title="Open a chat that walks you through filling this form">
            💬 Help me fill this out
        </button>

        <div class="jobs-form-grid">
            <label>Name
                <input id="job-form-name" type="text"
                       placeholder="my-hourly-recap" maxlength="64"
                       autocomplete="off" />
                <small>Letters, digits, hyphens, underscores. Becomes the filename.</small>
            </label>
            <label>Schedule
                <input id="job-form-schedule" type="text"
                       placeholder="0 * * * *" autocomplete="off"
                       oninput="onCronInput()" />
                <small id="job-form-cron-preview" class="cron-preview-hint">
                    5-field cron (MIN HOUR DOM MON DOW).
                    Example: <code>*/15 * * * *</code> = every 15 min.
                </small>
            </label>
            <label>Jitter
                <input id="job-form-jitter" type="number"
                       min="0" max="0" step="1"
                       placeholder="0" autocomplete="off"
                       disabled
                       oninput="onJitterInput()" />
                <small id="job-form-jitter-hint" class="cron-preview-hint">
                    Type a schedule to enable. Spreads phase-aligned starts.
                </small>
            </label>
            <label class="job-form-type-row">What does this job do?
                <select id="job-form-type" onchange="onJobTypeChange()">
                    <option value="prompt">Run a prompt — agent does a freeform task</option>
                    <option value="invoke">Invoke a capability or workflow</option>
                </select>
            </label>
        </div>

        <div id="job-form-prompt-row" class="job-form-row">
            <label>Prompt
                <textarea id="job-form-prompt" rows="4"
                          placeholder="What should the agent do when this fires?"></textarea>
            </label>
        </div>

        <div id="job-form-invoke-row" class="job-form-row" hidden>
            <label>Kind
                <select id="job-form-invoke-kind" onchange="onInvokeKindChange()">
                    <option value="capability">capability</option>
                    <option value="workflow">workflow</option>
                </select>
            </label>
            <label>
                <span id="job-form-invoke-name-label">Capability name</span>
                <input id="job-form-invoke-name" type="text" list="job-form-invoke-options"
                       placeholder="task_briefing" autocomplete="off"
                       oninput="onInvokeNameInput()" />
                <datalist id="job-form-invoke-options"></datalist>
                <small id="job-form-invoke-hint" class="cron-preview-hint"
                       style="min-height: 14px;"></small>
            </label>
            <label id="job-form-params-wrap" class="job-form-params-wrap">
                Params (JSON, optional)
                <textarea id="job-form-params" rows="3"
                          placeholder='{"same_day": true}'
                          oninput="onParamsInput()"></textarea>
                <small id="job-form-params-validity" class="cron-preview-hint">
                    Empty, or a JSON object.
                </small>
                <div id="job-form-params-schema" class="job-form-params-schema" hidden></div>
            </label>
        </div>

        <div id="job-form-error" class="job-form-error" hidden></div>
        <div class="job-form-actions">
            <button type="button" class="jobs-form-cancel"
                    onclick="hideAddJobForm()">Cancel</button>
            <button type="button" class="jobs-form-submit"
                    onclick="submitAddJobForm()">Create job</button>
        </div>
    </div>

    <!-- Pending-action banners (Created / Updated / Deleted). Lives
         outside #jobs-user so its rendering is decoupled from the
         /api/state-driven table refresh — banners appear instantly on
         the user's action without waiting for a fresh server read. -->
    <div id="jobs-pending-banners"></div>

    <div id="jobs-user"><div class="loading">Loading...</div></div>

    <details class="jobs-system-details" data-wb-detail-key="jobs-system" style="margin-top: 24px;">
        <summary>System Jobs</summary>
        <div id="jobs-system"><div class="loading">Loading...</div></div>
    </details>
</div>

<!-- CHATS -->
<div class="tab-panel" id="panel-chats">
    <!-- Toolbar: search + project + sort + window + Advanced toggle.
         Project lives in the main toolbar because it's the most-used
         filter (multi-repo users scan their work by repo constantly).
         Pure pills (has_commits, has_unfinished) live under Advanced
         since they're rarely-used power-user filters. -->
    <div class="chats-toolbar">
        <!-- Search input with a subtle inline send affordance pinned
             to the right edge. The button shares a parent so it
             absolute-positions inside the input's frame; it's
             tooltip-only labeled on hover ("Send · Enter") to avoid
             cluttering the toolbar with another full-size widget.
             The 1500ms as-you-type debounce + Enter still fire the
             same chatsGlobalSearch() — the send button is a third
             redundant path that exists purely as a discoverability
             affordance for the live-search behavior. -->
        <div class="chats-search-input-wrap">
            <input type="text" id="chats-global-search" class="chats-search-input"
                   placeholder="Search or filter the chats below..." />
            <button class="chats-search-send" id="chats-search-send"
                    onclick="chatsGlobalSearch()" aria-label="Search"
                    data-tooltip="Send · Enter">↵</button>
        </div>
        <select id="chats-search-method" class="chats-select" onchange="chatsSearchMethodChanged(this.value)">
            <option value="keyword,semantic">Hybrid</option>
            <option value="keyword">Keyword</option>
            <option value="semantic">Semantic</option>
            <option value="substring">Exact match</option>
        </select>
        <select id="chats-project-filter" class="chats-select chats-project-select"
                onchange="chatsProjectFilterChanged(this.value)">
            <option value="">All repos</option>
        </select>
        <span class="chats-toolbar-spacer"></span>
        <select id="chats-sort" class="chats-select" onchange="applyChatsFiltersAndSort()">
            <option value="recent">Most Recent</option>
            <option value="longest">Longest Duration</option>
            <option value="most-messages">Most Messages</option>
            <option value="most-commits">Most Commits</option>
            <option value="most-recent-commit">Most Recent Commit</option>
        </select>
        <select id="chats-days" class="chats-select">
            <option value="7">7 days</option>
            <option value="14">14 days</option>
            <option value="30" selected>30 days</option>
            <option value="60">60 days</option>
            <option value="0">All time</option>
        </select>
        <button class="chats-select chats-advanced-toggle" id="chats-advanced-toggle"
                onclick="chatsToggleAdvanced()">Advanced ▾</button>
    </div>

    <!-- Advanced filters expander (collapsed by default). Holds only
         the rarely-used pure-predicate pills. Project + sort + window
         are common enough to stay in the main toolbar. -->
    <div id="chats-advanced" class="chats-advanced-panel" style="display:none;">
        <div class="chats-filter-row">
            <span class="chats-filter-label">Filter:</span>
            <button class="chats-filter-pill" id="chats-pill-has-commits"
                    onclick="chatsToggleFilter('has_commits')">Has commits</button>
            <button class="chats-filter-pill" id="chats-pill-has-unfinished"
                    onclick="chatsToggleFilter('has_unfinished')">Has unfinished work</button>
            <span class="chats-filter-spacer"></span>
            <button class="chats-filter-pill chats-filter-reset" id="chats-pill-reset"
                    onclick="chatsResetFilters()" style="display:none;">Reset</button>
        </div>
    </div>

    <!-- Single-pane content area. Exactly one of #chats-list or
         #chats-viewer is visible at a time; selecting a chat replaces
         the list, the close button restores it.

         Search results render INTO #chats-list (re-ranked + chunk
         snippets per matching card) — there is no separate
         search-results pane. -->

    <div class="chats-content">
        <div id="chats-list" class="chats-list-fullwidth">
            <div class="loading">Loading chats...</div>
        </div>
        <!-- Numbered pager for the listing. Rendered by wbRenderPager
             from core/pager.py — same component costs > sessions uses. -->
        <div id="chats-pager" class="wb-pager"></div>

        <div id="chats-viewer" class="chats-viewer-fullwidth" style="display:none;">
            <!-- Back-to-list bar. Lives ABOVE the viewer header so the
                 "x close" affordance doesn't overlap the role-filter
                 buttons (User / Assistant / All) on the header's right
                 edge. Click anywhere on the bar or hit Esc to return. -->
            <div class="chats-viewer-backbar">
                <button class="chats-back-btn" onclick="closeChat()"
                        title="Back to chat list (Esc)">
                    <span class="chats-back-icon">‹</span> Back to all chats
                </button>
            </div>
            <div class="chats-viewer-header" id="chats-viewer-header"></div>
            <!-- In-chat search (toggleable). The hits row sits BELOW
                 the input + buttons (see styles.py: chats-in-search-hits
                 has flex-basis:100% so it wraps to its own line). -->
            <div class="chats-in-search" id="chats-in-search" style="display:none;">
                <div class="chats-in-search-bar">
                    <input type="text" id="chats-in-search-input" placeholder="Search in this chat..." />
                    <button onclick="chatsInSessionSearch()">Find</button>
                    <button onclick="chatsCloseInSearch()">Close</button>
                </div>
                <div id="chats-in-search-hits" class="chats-in-search-hits"></div>
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
                 collapses. See tabs/costs.py for behaviour. -->
            <span id="costs-rate-chip" class="costs-rate-chip"
                  style="display: none;"
                  onclick="costsToggleRateLimitPopover(event)"
                  title="Click for per-model breakdown">
                Limits: <span id="costs-rate-pct">—</span>
            </span>
            <div id="costs-rate-popover" class="costs-rate-popover" style="display: none;"></div>
            <span id="costs-meta" class="costs-meta"></span>
            <button class="chats-accent-btn" onclick="costsRefresh(this)">Refresh</button>
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
    <div id="costs-sessions-pager" class="wb-pager"></div>

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
    <div style="display:flex; gap:24px; align-items:flex-start; min-height:500px;">
        <div id="projects-list" style="flex:0 0 340px; position:sticky; top:16px; max-height:calc(100vh - 32px); overflow-y:auto;">
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
