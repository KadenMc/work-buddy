"""Dashboard Chats tab JS — session viewer with global + in-session search.

Owns the Chats tab loader plus all chat-related view code: chat list,
selected-session view, message rendering, role/project filters, the
in-session search with hit jumping, the cross-session global search
(literal and commit-search modes), and the commits sidebar.

Largest single tab in the dashboard (~1000 JS lines).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Chats ----
const chatsState = {
    chats: [],
    selectedId: null,
    messages: [],
    offset: 0,
    limit: 20,
    filteredCount: 0,
    hasMore: false,
    expandedMessages: new Set(),
    commits: [],
    searchHits: [],
    roleFilter: null,
};

/** Strip XML-like tags from message text (e.g. <command-name>...</command-name>) */
function cleanMsgText(text) {
    if (!text) return '';
    return text.replace(/<\/?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?>/g, '');
}

async function loadChats() {
    const days = document.getElementById('chats-days')?.value || 14;
    const data = await fetchJSON('/api/chats?days=' + days);
    if (!data) return;
    chatsState.chats = data.chats || [];
    renderChatList();
    chatsPopulateProjectFilter();

    // One-shot restore: if a `ci` URL key was stashed by _initFromHash,
    // resolve it (short_id → full session_id) and select that chat. The
    // key is cleared after consumption so subsequent loadChats() calls
    // (e.g. tab re-entry) don't re-apply stale state.
    if (window._urlState && window._urlState.ci) {
        const target = window._urlState.ci;
        delete window._urlState.ci;
        let resolved = null;
        // Short ID (8 chars) — look for unique match by short_id field.
        if (target.length <= 8) {
            const matches = chatsState.chats.filter(c => c.short_id === target);
            if (matches.length === 1) resolved = matches[0].session_id;
        }
        // Fall back to direct session_id match (covers full UUID and the
        // collision-fallback case where _persistHash wrote the full ID).
        if (!resolved && chatsState.chats.find(c => c.session_id === target)) {
            resolved = target;
        }
        if (resolved) {
            selectChat(resolved);
        } else {
            console.warn('[hash-restore] chat session not found:', target);
        }
    }
}

function renderChatList() {
    const container = document.getElementById('chats-list');
    let chats = [...chatsState.chats];

    // Filter by selected project
    var projectFilter = document.getElementById('chats-project-filter')?.value;
    if (projectFilter) {
        chats = chats.filter(function(c) { return c.project_name === projectFilter; });
    }

    const sort = document.getElementById('chats-sort')?.value || 'recent';
    if (sort === 'longest') chats.sort((a, b) => (b.message_count - a.message_count));
    else if (sort === 'most-messages') chats.sort((a, b) => (b.tool_count - a.tool_count));

    if (chats.length === 0) {
        container.innerHTML = '<div class="empty-state">No chats found</div>';
        return;
    }

    // 60-minute window: a chat whose latest activity is within the
    // last hour gets the same green pulse dot the Costs view uses for
    // "active session." Prefer end_time; fall back to start_time when
    // the data lacks an end_time.
    const ACTIVE_WINDOW_MS = 60 * 60 * 1000;

    container.innerHTML = chats.map(c => {
        // Title prefers the cached LLM tldr (when summaries are
        // enabled and the row exists); falls back to the first user
        // message so chat-only sessions still get a meaningful title.
        const titleSrc = c.tldr || c.first_message || 'Untitled chat';
        const title = escapeHtml(cleanMsgText(titleSrc).trim());
        const lastTs = c.end_time || c.start_time || '';
        const lastMs = lastTs ? new Date(lastTs).getTime() : NaN;
        const isActive = isFinite(lastMs) && (Date.now() - lastMs) < ACTIVE_WINDOW_MS;
        const activeDot = isActive
            ? '<span class="wb-active-dot" title="Active in the last hour"></span>'
            : '';
        return '<div class="chat-card' + (c.session_id === chatsState.selectedId ? ' active' : '') + '"'
            + ' data-sid="' + c.session_id + '">'
            + (c.project_name ? '<div class="chat-card-project">' + escapeHtml(c.project_name) + '</div>' : '')
            + '<div class="chat-card-title">' + title + '</div>'
            + '<div class="chat-card-meta">'
            + '<span>' + activeDot + formatTimestamp(c.start_time) + '</span>'
            + '<span>' + (c.duration || '--') + '</span>'
            + '<span>' + c.message_count + ' msgs</span>'
            + '</div>'
            + renderChatBadges(c)
            + '</div>';
    }).join('');

    container.querySelectorAll('.chat-card').forEach(function(card) {
        card.addEventListener('click', function() { selectChat(card.dataset.sid); });
    });
}

/**
 * Render the commit + unfinished-work badge row for a chat card.
 * Only renders when the session demonstrably engages with git
 * (committed something OR wrote files via Write/Edit/NotebookEdit) —
 * keeps cards slim for chat-only sessions where git noise is moot.
 */
function renderChatBadges(c) {
    if (!c.engages_git) return '';
    var parts = [];
    if (c.commit_count > 0) {
        var repoCount = c.commits_by_repo ? Object.keys(c.commits_by_repo).length : 0;
        var commitsLabel = '🌿 ' + c.commit_count + ' commit' + (c.commit_count === 1 ? '' : 's');
        if (repoCount > 1) commitsLabel += ' across ' + repoCount + ' repos';
        parts.push('<span class="chat-card-badge">' + commitsLabel + '</span>');
    }
    if (c.unfinished_count > 0) {
        var unfinishedLabel = '⚠ ' + c.unfinished_count + ' left uncommitted';
        parts.push('<span class="chat-card-badge unfinished" title="Files this session wrote that this session did not commit itself.">' + unfinishedLabel + '</span>');
    }
    if (parts.length === 0) return '';
    return '<div class="chat-card-badges">' + parts.join('') + '</div>';
}

async function selectChat(sessionId) {
    chatsState.selectedId = sessionId;
    _persistHash();
    chatsState.offset = 0;
    chatsState._earliestLoaded = 0;
    chatsState.messages = [];
    chatsState.expandedMessages.clear();
    chatsState.commits = [];
    chatsState.searchHits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    // Single-pane swap: hide the listing, show the viewer at full
    // width. The close button (X) is wired to closeChat() to swap
    // back. URL hash already tracks selectedId so reload restores
    // the same view.
    document.getElementById('chats-list').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-in-search').style.display = 'none';
    document.getElementById('chats-commits-bar').style.display = 'none';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Loading messages...</div>';

    const [msgData, commitData] = await Promise.all([
        fetchJSON('/api/chats/' + sessionId + '/messages?offset=0&limit=' + chatsState.limit),
        fetchJSON('/api/chats/' + sessionId + '/commits'),
    ]);

    if (msgData) {
        chatsState.messages = msgData.messages || [];
        chatsState.filteredCount = msgData.filtered_count || 0;
        chatsState.hasMore = msgData.has_more || false;
        chatsState.offset = chatsState.messages.length;
        renderChatHeader(msgData.metadata);
    }

    if (commitData && commitData.commits && commitData.commits.length > 0) {
        chatsState.commits = commitData.commits;
        renderCommitsBar(commitData.commits);
    }

    renderMessages();
}

/**
 * Swap back from the chat-detail view to the listing. Clears the
 * selected-chat URL state so a reload lands on the listing rather
 * than re-opening the same session.
 */
function closeChat() {
    chatsState.selectedId = null;
    _persistHash();
    document.getElementById('chats-viewer').style.display = 'none';
    document.getElementById('chats-list').style.display = 'flex';
    document.querySelectorAll('.chat-card.active').forEach(function(c) {
        c.classList.remove('active');
    });
}

// Esc closes the chat-detail view when the panel is active and the
// user isn't typing into an input. Mirrors the Esc behavior on
// modal-style panels elsewhere in the dashboard.
document.addEventListener('keydown', function(ev) {
    if (ev.key !== 'Escape') return;
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    var viewer = document.getElementById('chats-viewer');
    if (!viewer || viewer.style.display === 'none') return;
    var panel = document.getElementById('panel-chats');
    if (!panel || !panel.classList.contains('active')) return;
    closeChat();
});

function renderChatHeader(meta) {
    if (!meta) return;
    var header = document.getElementById('chats-viewer-header');
    header.innerHTML = '<div class="chats-hdr-left">'
        + '<code>' + (meta.session_id ? meta.session_id.substring(0, 8) : '--') + '</code>'
        + ' &middot; ' + (meta.message_count || 0) + ' messages'
        + ' &middot; ' + (meta.duration || '--')
        + (meta.start_time ? ' &middot; ' + formatTimestamp(meta.start_time) : '')
        + '</div>'
        + '<div class="chats-hdr-right">'
        + '<button class="chats-hdr-btn" id="chats-hdr-resume" onclick="chatsResumeSession()" title="Open a new local terminal and resume this session (claude --resume). No prompt sent.">Resume</button>'
        + '<button class="chats-hdr-btn" onclick="chatsToggleInSearch()">Search</button>'
        + '<span class="chats-hdr-divider"></span>'
        + '<button class="chats-hdr-btn' + (chatsState.roleFilter === 'user' ? ' active' : '') + '" onclick="chatsFilterRole(&#39;user&#39;)">User</button>'
        + '<button class="chats-hdr-btn' + (chatsState.roleFilter === 'assistant' ? ' active' : '') + '" onclick="chatsFilterRole(&#39;assistant&#39;)">Assistant</button>'
        + '<button class="chats-hdr-btn' + (!chatsState.roleFilter ? ' active' : '') + '" onclick="chatsFilterRole(null)">All</button>'
        + '</div>';
}

async function chatsResumeSession() {
    var sid = chatsState.selectedId;
    if (!sid) return;
    if (!confirm('Open a new Claude Code terminal and resume this session? (claude --resume — no prompt sent, but a new window will appear on your desktop.)')) return;
    var btn = document.getElementById('chats-hdr-resume');
    var originalLabel = btn ? btn.textContent : 'Resume';
    if (btn) { btn.disabled = true; btn.textContent = 'Opening…'; }
    try {
        var resp = await fetch('/api/chats/' + encodeURIComponent(sid) + '/resume', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}',
        });
        var data = await resp.json().catch(function() { return {}; });
        if (!resp.ok || !data.success) {
            alert('Resume failed: ' + (data.error || resp.statusText));
            if (btn) { btn.textContent = originalLabel; btn.disabled = false; }
            return;
        }
        if (btn) { btn.textContent = 'Opened'; }
        setTimeout(function() {
            if (btn) { btn.textContent = originalLabel; btn.disabled = false; }
        }, 2000);
    } catch (err) {
        alert('Resume request failed: ' + err);
        if (btn) { btn.textContent = originalLabel; btn.disabled = false; }
    }
}

function renderMessages() {
    var container = document.getElementById('chats-message-list');
    if (chatsState.messages.length === 0) {
        container.innerHTML = '<div class="empty-state">No messages</div>';
        document.getElementById('chats-load-later').style.display = 'none';
        return;
    }

    var html = '';
    chatsState.messages.forEach(function(msg) {
        var isExpanded = chatsState.expandedMessages.has(msg.index);
        var inSpan = chatsState.searchHits.some(function(h) {
            return msg.index >= h.turn_range[0] && msg.index < h.turn_range[1];
        });
        var rawText = cleanMsgText(isExpanded ? (msg.text || msg.text_preview || '') : (msg.text_preview || msg.text || ''));
        var text = rawText.substring(0, isExpanded ? 100000 : 300);
        var truncated = !isExpanded && rawText.length > 300;
        var hasText = text.trim().length > 0;
        var hasTools = msg.tools && msg.tools.length > 0;

        // Skip entirely empty turns (no text, no tools)
        if (!hasText && !hasTools) return;

        // Build tool badges string (reused in bubble or meta)
        var toolBadges = hasTools
            ? msg.tools.map(function(t) { return '<span class="chat-msg-tool-badge">' + t + '</span>'; }).join('')
            : '';

        // For tool-only turns, show tools inside the bubble instead of empty
        var bubbleContent = '';
        if (hasText) {
            bubbleContent = escapeHtml(text)
                + (truncated ? '<span class="chat-msg-truncated"> ...</span>' : '');
        } else {
            bubbleContent = '<div class="chat-msg-tools" style="margin:0;">' + toolBadges + '</div>';
        }

        html += '<div class="chat-msg ' + msg.role + '">'
            + '<div class="chat-msg-bubble'
            + (isExpanded ? ' expanded' : '')
            + (inSpan ? ' in-span' : '')
            + '" data-idx="' + msg.index + '" onclick="chatsMsgClick(' + msg.index + ')">'
            + bubbleContent
            + '</div>'
            + '<div class="chat-msg-meta">'
            + (msg.timestamp ? formatTimestamp(msg.timestamp) : '')
            + (hasText && msg.role === 'assistant' && hasTools
                ? ' <div class="chat-msg-tools">' + toolBadges + '</div>'
                : '')
            + '</div></div>';
    });

    container.innerHTML = html;

    document.getElementById('chats-load-later').style.display =
        chatsState.hasMore ? 'block' : 'none';

    // Show load-earlier if we jumped into the middle of the conversation
    var earliest = chatsState._earliestLoaded || 0;
    document.getElementById('chats-load-earlier').style.display =
        earliest > 0 ? 'block' : 'none';
}

async function chatsMsgClick(index) {
    if (chatsState.expandedMessages.has(index)) {
        chatsState.expandedMessages.delete(index);
        renderMessages();
        return;
    }

    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/expand/' + index + '?context_window=0'
    );
    if (!data || !data.messages) return;

    var fullMsg = data.messages.find(function(m) { return m.is_center; });
    if (fullMsg) {
        var existing = chatsState.messages.find(function(m) { return m.index === index; });
        if (existing) {
            existing.text = fullMsg.text;
        }
    }
    chatsState.expandedMessages.add(index);
    renderMessages();
}

async function chatsLoadLater() {
    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/messages?offset=' + chatsState.offset
        + '&limit=' + chatsState.limit
        + (chatsState.roleFilter ? '&roles=' + chatsState.roleFilter : '')
    );
    if (!data) return;
    chatsState.messages = chatsState.messages.concat(data.messages || []);
    chatsState.hasMore = data.has_more || false;
    chatsState.offset += (data.messages || []).length;

    // Preserve scroll position — content added below, so same scrollTop works
    var scroller = document.getElementById('chats-messages');
    var prevScroll = scroller.scrollTop;
    renderMessages();
    scroller.scrollTop = prevScroll;
}

async function chatsLoadEarlier() {
    var earliest = chatsState._earliestLoaded || 0;
    if (earliest <= 0) return;

    var newOffset = Math.max(0, earliest - chatsState.limit);
    var newLimit = earliest - newOffset;
    if (newLimit <= 0) return;

    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=' + newOffset + '&limit=' + newLimit;
    if (chatsState.roleFilter) url += '&roles=' + chatsState.roleFilter;

    var data = await fetchJSON(url);
    if (!data) return;

    var newMsgs = data.messages || [];
    if (newMsgs.length === 0) return;

    // Prepend to existing messages
    chatsState.messages = newMsgs.concat(chatsState.messages);
    chatsState._earliestLoaded = newOffset;

    // Preserve scroll position — content added above, so offset by the height delta
    var scroller = document.getElementById('chats-messages');
    var list = document.getElementById('chats-message-list');
    var prevHeight = list.scrollHeight;
    var prevScroll = scroller.scrollTop;
    renderMessages();
    var newHeight = list.scrollHeight;
    scroller.scrollTop = prevScroll + (newHeight - prevHeight);
}

// ---- Chats: Global search ----

// Track the last global search query so we can carry it into in-chat search
var _lastGlobalQuery = '';
var _commitsPrepared = false;
var _commitsPreparedProject = '';

function chatsPopulateProjectFilter() {
    var select = document.getElementById('chats-project-filter');
    var current = select.value;

    // Collect distinct project names from loaded chats
    var projects = {};
    chatsState.chats.forEach(function(c) {
        if (c.project_name) projects[c.project_name] = (projects[c.project_name] || 0) + 1;
    });

    var sorted = Object.keys(projects).sort();
    var html = '<option value="">All repos</option>';
    sorted.forEach(function(p) {
        html += '<option value="' + escapeHtml(p) + '">' + escapeHtml(p) + ' (' + projects[p] + ')</option>';
    });
    select.innerHTML = html;

    // Restore previous selection if still valid
    if (current && projects[current]) select.value = current;

    chatsUpdateCommitOption();
}

function chatsProjectFilterChanged(project) {
    var select = document.getElementById('chats-project-filter');
    select.classList.toggle('active', !!project);

    chatsUpdateCommitOption();

    // Reset commit cache when project changes
    if (_commitsPreparedProject !== project) {
        _commitsPrepared = false;
        _commitsPreparedProject = '';
    }

    // If Commit method is selected but now hidden, fall back to Hybrid
    var methodSelect = document.getElementById('chats-search-method');
    if (methodSelect.value === 'commit' && !project) {
        methodSelect.value = 'keyword,semantic';
        chatsSearchMethodChanged('keyword,semantic');
    }

    // Re-render chat list with project filter applied
    renderChatList();
}

function chatsUpdateCommitOption() {
    var project = document.getElementById('chats-project-filter').value;
    var methodSelect = document.getElementById('chats-search-method');

    // Add or remove Commit option based on project selection
    var commitOpt = methodSelect.querySelector('option[value="commit"]');
    if (project) {
        if (!commitOpt) {
            commitOpt = document.createElement('option');
            commitOpt.value = 'commit';
            commitOpt.textContent = 'Commit';
            methodSelect.appendChild(commitOpt);
        }
    } else {
        if (commitOpt) commitOpt.remove();
    }
}

function chatsSearchMethodChanged(method) {
    var input = document.getElementById('chats-global-search');
    var project = document.getElementById('chats-project-filter').value;

    if (method === 'commit') {
        input.placeholder = 'Search by commit hash or message...';
        // Pre-embed commits for the selected project
        if (!_commitsPrepared || _commitsPreparedProject !== project) {
            _commitsPrepared = true;
            _commitsPreparedProject = project;
            var prepareUrl = '/api/chats/commits/prepare'
                + (project ? '?project=' + encodeURIComponent(project) : '');
            fetch(prepareUrl, { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d && d.commit_count) {
                        console.log('Commit embeddings warmed: ' + d.commit_count + ' commits');
                    }
                }).catch(function() {});
        }
    } else {
        input.placeholder = project
            ? 'Search in ' + project + '...'
            : 'Search across all chats...';
    }
}

async function chatsGlobalSearch() {
    var q = document.getElementById('chats-global-search').value.trim();
    if (!q) {
        document.getElementById('chats-search-results').style.display = 'none';
        return;
    }
    _lastGlobalQuery = q;

    var method = document.getElementById('chats-search-method').value;
    if (method === 'commit') return chatsCommitSearch(q);

    var resultsDiv = document.getElementById('chats-search-results');
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = '<div class="loading">Searching...</div>';

    var project = document.getElementById('chats-project-filter').value;
    var data = await fetchJSON(
        '/api/chats/search?q=' + encodeURIComponent(q) + '&method=' + method
        + (project ? '&project=' + encodeURIComponent(project) : '')
    );

    if (!data || data.error) {
        resultsDiv.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Search failed') + '</div>';
        return;
    }

    // Server returns {sessions: [...], total_chunks: N} — already grouped and scored
    var sessions = data.sessions || [];
    if (sessions.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No results found</div>';
        return;
    }

    // Enrich with data from the chat list (first_message, duration, msg count)
    var chatLookup = {};
    chatsState.chats.forEach(function(c) { chatLookup[c.session_id] = c; });

    var html = '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        + '<span style="font-size:12px;color:var(--text-muted);">'
        + sessions.length + ' chat' + (sessions.length !== 1 ? 's' : '')
        + ' (' + (data.total_chunks || 0) + ' chunks) for "' + escapeHtml(q) + '"</span>'
        + '<button class="chats-hdr-btn" onclick="chatsCloseGlobalSearch()">Close</button>'
        + '</div>';

    sessions.forEach(function(sess, gi) {
        var chatInfo = chatLookup[sess.session_id];
        var title = chatInfo
            ? escapeHtml(cleanMsgText(chatInfo.first_message || '').split('\\n')[0].substring(0, 80))
            : '';
        var duration = chatInfo ? (chatInfo.duration || '') : '';
        var msgCount = chatInfo ? (chatInfo.message_count || '') : '';
        var timeStr = sess.start_time ? formatTimestamp(sess.start_time) : '';

        // Session header — clicking opens the chat at the top
        html += '<div class="chats-search-session-group">'
            + '<div class="chats-search-session-hdr" onclick="chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span>'
            + '<span class="chats-hit-score" style="margin-right:6px;">#' + (gi + 1) + '</span>'
            + '<code style="color:var(--text-primary);font-size:11px;">' + sess.short_id + '</code>'
            + (sess.project_name ? ' <span style="color:var(--accent);font-size:10px;font-weight:600;text-transform:uppercase;margin-left:6px;">' + escapeHtml(sess.project_name) + '</span>' : '')
            + '</span>'
            + '<span style="font-size:11px;color:var(--text-muted);">'
            + sess.chunks.length + ' hit' + (sess.chunks.length !== 1 ? 's' : '')
            + (duration ? ' &middot; ' + duration : '')
            + (msgCount ? ' &middot; ' + msgCount + ' msgs' : '')
            + (timeStr ? ' &middot; ' + timeStr : '')
            + '</span></div>'
            + (title ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + title + '</div>' : '')
            + '</div>';

        // Individual chunk hits — clicking jumps to that exact location
        (sess.chunks || []).forEach(function(chunk) {
            html += '<div class="chats-search-chunk" onclick="chatsJumpToHit(&#39;' + sess.session_id + '&#39;,' + chunk.span_index + ')">'
                + '<div style="font-size:12px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                + escapeHtml(cleanMsgText(chunk.display_text)) + '</div>'
                + '</div>';
        });

        html += '</div>';
    });

    resultsDiv.innerHTML = html;
}

function chatsCloseGlobalSearch() {
    document.getElementById('chats-search-results').style.display = 'none';
}

async function chatsCommitSearch(q) {
    var resultsDiv = document.getElementById('chats-search-results');
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = '<div class="loading">Searching commits...</div>';

    var project = document.getElementById('chats-project-filter').value;
    var url = '/api/chats/search/commits?q=' + encodeURIComponent(q)
        + (project ? '&project=' + encodeURIComponent(project) : '');
    var data = await fetchJSON(url);

    if (!data || data.error) {
        resultsDiv.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Commit search failed') + '</div>';
        return;
    }

    var sessions = data.sessions || [];
    if (sessions.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No matching commits found</div>';
        return;
    }

    var chatLookup = {};
    chatsState.chats.forEach(function(c) { chatLookup[c.session_id] = c; });

    var totalCommits = data.total_commits || 0;
    var html = '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        + '<span style="font-size:12px;color:var(--text-muted);">'
        + totalCommits + ' commit' + (totalCommits !== 1 ? 's' : '')
        + ' in ' + sessions.length + ' chat' + (sessions.length !== 1 ? 's' : '')
        + ' for "' + escapeHtml(q) + '"</span>'
        + '<button class="chats-hdr-btn" onclick="chatsCloseGlobalSearch()">Close</button>'
        + '</div>';

    sessions.forEach(function(sess, gi) {
        var chatInfo = chatLookup[sess.session_id];
        var title = chatInfo
            ? escapeHtml(cleanMsgText(chatInfo.first_message || '').split('\\n')[0].substring(0, 80))
            : '';
        var duration = chatInfo ? (chatInfo.duration || '') : '';
        var msgCount = chatInfo ? (chatInfo.message_count || '') : '';

        html += '<div class="chats-search-session-group">'
            + '<div class="chats-search-session-hdr" onclick="chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span>'
            + '<span class="chats-hit-score" style="margin-right:6px;">#' + (gi + 1) + '</span>'
            + '<code style="color:var(--text-primary);font-size:11px;">' + sess.short_id + '</code>'
            + '</span>'
            + '<span style="font-size:11px;color:var(--text-muted);">'
            + sess.commits.length + ' commit' + (sess.commits.length !== 1 ? 's' : '')
            + (duration ? ' &middot; ' + duration : '')
            + (msgCount ? ' &middot; ' + msgCount + ' msgs' : '')
            + '</span></div>'
            + (title ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + title + '</div>' : '')
            + '</div>';

        (sess.commits || []).forEach(function(commit) {
            var hasIdx = commit.message_index != null;
            var clickFn = hasIdx
                ? 'chatsJumpToCommitSearch(&#39;' + sess.session_id + '&#39;,' + commit.message_index + ')'
                : 'chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)';
            html += '<div class="chats-search-chunk" onclick="' + clickFn + '">'
                + '<div class="chat-commit-marker" style="margin:0;">'
                + '<code>' + (commit.hash || '') + '</code> '
                + '<span class="commit-msg">' + escapeHtml(commit.message || '') + '</span>'
                + '<span class="commit-meta">'
                + '<span>' + (commit.branch || '') + '</span>'
                + (commit.files_changed ? '<span>' + commit.files_changed + ' files</span>' : '')
                + '<span>' + formatTimestamp(commit.timestamp) + '</span>'
                + '</span></div></div>';
        });

        html += '</div>';
    });

    resultsDiv.innerHTML = html;
}

async function chatsJumpToCommitSearch(sessionId, messageIndex) {
    chatsCloseGlobalSearch();

    chatsState.selectedId = sessionId;
    _persistHash();
    chatsState.expandedMessages.clear();
    chatsState.searchHits = [];
    chatsState.commits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    document.getElementById('chats-list').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Jumping to commit...</div>';

    var contextWindow = Math.floor(chatsState.limit / 2);
    var offset = Math.max(0, messageIndex - contextWindow);
    var url = '/api/chats/' + sessionId + '/messages?offset=' + offset + '&limit=' + chatsState.limit;

    var msgData = await fetchJSON(url);
    if (!msgData || !msgData.messages || msgData.messages.length === 0) {
        document.getElementById('chats-message-list').innerHTML =
            '<div class="empty-state">Failed to load messages</div>';
        return;
    }

    chatsState.messages = msgData.messages;
    chatsState.filteredCount = msgData.filtered_count || 0;
    chatsState.hasMore = msgData.has_more || false;

    var msgs = chatsState.messages;
    chatsState.offset = msgs[msgs.length - 1].index + 1;
    chatsState._earliestLoaded = msgs[0].index;

    renderChatHeader(msgData.metadata);

    // Load commits bar in background
    fetchJSON('/api/chats/' + sessionId + '/commits').then(function(commitData) {
        if (commitData && commitData.commits && commitData.commits.length > 0) {
            chatsState.commits = commitData.commits;
            renderCommitsBar(commitData.commits);
        }
    });

    renderMessages();

    setTimeout(function() {
        var target = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.style.transition = 'box-shadow 0.3s';
            target.style.boxShadow = '0 0 0 3px #3fb950';
            setTimeout(function() { target.style.boxShadow = ''; }, 1500);
        }
    }, 150);
}

/** Open a chat from search results — loads from the top and carries the query into in-chat search */
async function chatsOpenFromSearch(sessionId) {
    chatsCloseGlobalSearch();
    await selectChat(sessionId);

    // Carry the search query into the in-chat search bar
    if (_lastGlobalQuery) {
        document.getElementById('chats-in-search-input').value = _lastGlobalQuery;
        document.getElementById('chats-in-search').style.display = 'flex';
    }
}

/** Jump to a specific chunk within a session from search results */
async function chatsJumpToHit(sessionId, spanIndex) {
    chatsCloseGlobalSearch();

    chatsState.selectedId = sessionId;
    _persistHash();
    chatsState.expandedMessages.clear();
    chatsState.searchHits = [];
    chatsState.commits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    document.getElementById('chats-list').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Jumping to result...</div>';

    // Carry the search query into the in-chat search bar
    if (_lastGlobalQuery) {
        document.getElementById('chats-in-search-input').value = _lastGlobalQuery;
        document.getElementById('chats-in-search').style.display = 'flex';
    }

    var data = await fetchJSON('/api/chats/' + sessionId + '/locate/' + spanIndex);
    if (!data || data.error) {
        document.getElementById('chats-message-list').innerHTML =
            '<div class="empty-state">' + (data && data.error ? escapeHtml(data.error) : 'Failed to locate') + '</div>';
        return;
    }

    chatsState.messages = data.messages || [];
    chatsState.filteredCount = data.total_messages || 0;
    if (data.span_turn_range) {
        chatsState.searchHits = [{ turn_range: data.span_turn_range }];
    }

    // Set pagination state based on the window of messages returned
    var msgs = chatsState.messages;
    if (msgs.length > 0) {
        var firstIdx = msgs[0].index;
        var lastIdx = msgs[msgs.length - 1].index;
        chatsState.hasMore = lastIdx < (chatsState.filteredCount - 1);
        chatsState.offset = lastIdx + 1;
        chatsState._earliestLoaded = firstIdx;
    } else {
        chatsState.hasMore = false;
        chatsState.offset = 0;
        chatsState._earliestLoaded = 0;
    }

    renderChatHeader(data.metadata);
    renderMessages();

    // Also fetch commits in the background
    fetchJSON('/api/chats/' + sessionId + '/commits').then(function(commitData) {
        if (commitData && commitData.commits && commitData.commits.length > 0) {
            chatsState.commits = commitData.commits;
            renderCommitsBar(commitData.commits);
        }
    });

    setTimeout(function() {
        var spanEl = document.querySelector('.chat-msg-bubble.in-span');
        if (spanEl) spanEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 150);
}

// ---- Chats: In-session search ----

function chatsToggleInSearch() {
    var el = document.getElementById('chats-in-search');
    if (el.style.display === 'none' || el.style.display === '') {
        el.style.display = 'flex';
        document.getElementById('chats-in-search-input').focus();
    } else {
        el.style.display = 'none';
    }
}

function chatsCloseInSearch() {
    document.getElementById('chats-in-search').style.display = 'none';
    chatsState.searchHits = [];
    document.getElementById('chats-in-search-hits').innerHTML = '';
    renderMessages();
}

async function chatsInSessionSearch() {
    var q = document.getElementById('chats-in-search-input').value.trim();
    if (!q || !chatsState.selectedId) return;

    var hitsDiv = document.getElementById('chats-in-search-hits');
    hitsDiv.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">Searching...</span>';

    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/search?q=' + encodeURIComponent(q)
    );

    if (!data || data.error) {
        hitsDiv.innerHTML = '<span style="color:#f85149;font-size:12px;">'
            + (data && data.error ? escapeHtml(data.error) : 'Search failed') + '</span>';
        return;
    }

    var hits = data.hits || [];
    chatsState.searchHits = hits;

    if (hits.length === 0) {
        hitsDiv.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">No results</span>';
        renderMessages();
        return;
    }

    // Build a rich hit list with snippets from each span
    var html = '<div style="width:100%;margin-top:4px;">';
    html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">'
        + hits.length + ' match' + (hits.length !== 1 ? 'es' : '') + ' found</div>';

    hits.forEach(function(h, i) {
        // Extract a snippet from the first user message in the span
        var snippet = '';
        if (h.messages && h.messages.length > 0) {
            for (var mi = 0; mi < h.messages.length; mi++) {
                var mtxt = cleanMsgText(h.messages[mi].text || h.messages[mi].text_preview || '');
                if (mtxt.length > 10) {
                    snippet = mtxt.substring(0, 120);
                    break;
                }
            }
        }
        if (!snippet) snippet = 'Messages ' + h.turn_range[0] + '-' + h.turn_range[1];

        html += '<div class="chats-search-hit" style="padding:6px 8px;cursor:pointer;" onclick="chatsJumpToInHit(' + i + ')">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span style="font-size:11px;font-weight:600;color:var(--accent);">#' + (i + 1) + '</span>'
            + '<span style="font-size:10px;color:var(--text-muted);">msgs ' + h.turn_range[0] + '\u2013' + h.turn_range[1] + '</span>'
            + '</div>'
            + '<div style="font-size:12px;color:var(--text-secondary);margin-top:2px;'
            + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
            + escapeHtml(snippet) + '</div></div>';
    });
    html += '</div>';
    hitsDiv.innerHTML = html;

    // Reload messages to include the full conversation so highlights work
    var msgData = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/messages?offset=0&limit=200'
    );
    if (msgData) {
        chatsState.messages = msgData.messages || [];
        chatsState.filteredCount = msgData.filtered_count || 0;
        chatsState.hasMore = msgData.has_more || false;
        chatsState.offset = chatsState.messages.length;
    }
    renderMessages();

    // Scroll to first hit
    if (hits.length > 0 && hits[0].turn_range) {
        setTimeout(function() {
            var el = document.querySelector('.chat-msg-bubble[data-idx="' + hits[0].turn_range[0] + '"]');
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 150);
    }
}

function chatsJumpToInHit(hitIndex) {
    var hit = chatsState.searchHits[hitIndex];
    if (!hit || !hit.turn_range) return;
    // Scroll to the first message in this span
    var el = document.querySelector('.chat-msg-bubble[data-idx="' + hit.turn_range[0] + '"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Flash animation to draw attention
        el.style.transition = 'box-shadow 0.3s';
        el.style.boxShadow = '0 0 0 3px var(--accent)';
        setTimeout(function() { el.style.boxShadow = ''; }, 1500);
    }
}

// ---- Chats: Role filter ----

async function chatsFilterRole(role) {
    chatsState.roleFilter = role;
    chatsState.offset = 0;
    chatsState.messages = [];
    chatsState.expandedMessages.clear();

    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=0&limit=' + chatsState.limit;
    if (role) url += '&roles=' + role;

    var data = await fetchJSON(url);
    if (!data) return;
    chatsState.messages = data.messages || [];
    chatsState.filteredCount = data.filtered_count || 0;
    chatsState.hasMore = data.has_more || false;
    chatsState.offset = chatsState.messages.length;
    renderChatHeader(data.metadata);
    renderMessages();
}

// ---- Chats: Commits bar ----

function renderCommitsBar(commits) {
    var bar = document.getElementById('chats-commits-bar');
    bar.style.display = 'block';

    // Group by message to deduplicate retried/amended commits
    var groups = [];
    var seen = {};
    commits.forEach(function(c) {
        var key = (c.message || '').trim();
        if (seen[key]) {
            seen[key].hashes.push(c.hash || '');
            seen[key].count++;
        } else {
            var g = {
                message: key, hashes: [c.hash || ''], branch: c.branch || '',
                files_changed: c.files_changed, count: 1,
                message_index: c.message_index != null ? c.message_index : null,
                timestamp: c.timestamp || '',
            };
            seen[key] = g;
            groups.push(g);
        }
    });

    // Sort chronologically (oldest first)
    groups.sort(function(a, b) { return (a.timestamp || '').localeCompare(b.timestamp || ''); });

    var html = '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">'
        + groups.length + ' unique commit' + (groups.length !== 1 ? 's' : '')
        + (commits.length !== groups.length ? ' (' + commits.length + ' total incl. retries)' : '')
        + ' during this session</div>';

    groups.forEach(function(g) {
        var clickable = g.message_index != null;
        var clickAttr = clickable
            ? ' onclick="chatsJumpToCommit(' + g.message_index + ')" title="Jump to this commit in the conversation"'
            : '';
        html += '<div class="chat-commit-marker' + (clickable ? ' clickable' : '') + '"' + clickAttr + '>'
            + '<code>' + g.hashes[0] + '</code> '
            + '<span class="commit-msg">' + escapeHtml(g.message) + '</span>'
            + '<span class="commit-meta">'
            + (g.count > 1 ? '<span>(' + g.count + 'x)</span>' : '')
            + '<span>' + g.branch + '</span>'
            + (g.files_changed ? '<span>' + g.files_changed + ' files</span>' : '')
            + '<span>' + formatTimestamp(g.timestamp) + '</span>'
            + '</span>'
            + '</div>';
    });
    bar.innerHTML = html;
}

async function chatsJumpToCommit(messageIndex) {
    // Check if the target message is already in the DOM
    var el = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.style.transition = 'box-shadow 0.3s';
        el.style.boxShadow = '0 0 0 3px #3fb950';
        setTimeout(function() { el.style.boxShadow = ''; }, 1500);
        return;
    }

    // Target not loaded — fetch a page centered on the commit's turn
    var contextWindow = Math.floor(chatsState.limit / 2);
    var offset = Math.max(0, messageIndex - contextWindow);
    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=' + offset + '&limit=' + chatsState.limit;
    if (chatsState.roleFilter) url += '&roles=' + chatsState.roleFilter;

    var data = await fetchJSON(url);
    if (!data || !data.messages || data.messages.length === 0) return;

    chatsState.messages = data.messages;
    chatsState.filteredCount = data.filtered_count || 0;
    chatsState.hasMore = data.has_more || false;

    var msgs = chatsState.messages;
    var firstIdx = msgs[0].index;
    var lastIdx = msgs[msgs.length - 1].index;
    chatsState.offset = lastIdx + 1;
    chatsState._earliestLoaded = firstIdx;

    renderMessages();

    // Scroll after DOM update
    setTimeout(function() {
        var target = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.style.transition = 'box-shadow 0.3s';
            target.style.boxShadow = '0 0 0 3px #3fb950';
            setTimeout(function() { target.style.boxShadow = ''; }, 1500);
        }
    }, 150);
}

// ---- Chats: Event listeners ----

document.getElementById('chats-sort')?.addEventListener('change', renderChatList);
document.getElementById('chats-days')?.addEventListener('change', function() { loadChats(); });
document.getElementById('chats-global-search')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') chatsGlobalSearch();
});
document.getElementById('chats-in-search-input')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') chatsInSessionSearch();
});
"""
