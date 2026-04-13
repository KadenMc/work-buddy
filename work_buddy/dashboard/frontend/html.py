"""Dashboard HTML structure."""

from __future__ import annotations


def _html() -> str:
    return """
<header class="header">
    <h1><span>work-buddy</span> dashboard</h1>
    <div class="header-meta">
        <span id="sidecar-status"><span class="status-dot stopped"></span> loading...</span>
        <span id="clock"></span>
        <span class="cp-kbd-hint" onclick="cpOpen()" title="Command palette">Ctrl+K</span>
    </div>
</header>

<nav class="tab-bar">
    <div class="tab-bar-left">
        <button class="tab-btn active" data-tab="overview">Overview</button>
        <button class="tab-btn" data-tab="tasks">Tasks</button>
        <button class="tab-btn" data-tab="status">Status</button>
        <button class="tab-btn" data-tab="chats">Chats</button>
        <button class="tab-btn" data-tab="contracts">Contracts</button>
        <button class="tab-btn" data-tab="projects">Projects</button>
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
        <div class="section-title"><a id="master-task-link" href="#" style="color: var(--accent); text-decoration: none;" title="Open in Obsidian">Master Task List</a></div>
        <input type="text" id="task-search" class="task-search-input" placeholder="Filter tasks..." />
    </div>
    <div id="task-list"><div class="loading">Loading tasks...</div></div>
</div>

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
