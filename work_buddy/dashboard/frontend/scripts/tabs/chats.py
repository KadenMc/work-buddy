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
    // Advanced filter pills. Each value is a boolean; True means
    // "session must satisfy this predicate to be shown." Compose AND.
    filters: { has_commits: false, has_unfinished: false },
    // Search state. When `searchActive`, `renderChatList` filters the
    // listing to only sessions returned by the IR engine, sorts them
    // by `doc_score`, and appends matched-chunk snippets to each card.
    // No separate search-results pane — the listing IS the search
    // result view.
    searchActive: false,
    searchQuery: '',
    searchSessionsByScore: [],
    // Pagination. The visible window is `(page+1) * pageSize` cards
    // from the head of the filtered+sorted list. Reset to 0 on any
    // filter / search / sort change so the user lands at the top.
    page: 0,
    pageSize: 25,
};

/** Strip XML-like tags from message text (e.g. <command-name>...</command-name>) */
function cleanMsgText(text) {
    if (!text) return '';
    return text.replace(/<\/?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?>/g, '');
}

async function loadChats() {
    // Honor `?days=N` from the URL hash if present (one-shot restore).
    // The hash machinery stashes hash params on window._urlState; we
    // consume `days` once and then defer to the dropdown's value for
    // subsequent loads.
    if (window._urlState && window._urlState.days) {
        const daysSel = document.getElementById('chats-days');
        if (daysSel) daysSel.value = window._urlState.days;
        delete window._urlState.days;
    }
    const days = document.getElementById('chats-days')?.value ?? 30;
    const data = await fetchJSON('/api/chats?days=' + days);
    if (!data) return;
    chatsState.chats = data.chats || [];
    chatsResetPage();
    chatsPopulateProjectFilter();

    // Honor `?q=...` one-shot. Triggers a search after data lands so
    // the search corpus is the freshly-loaded chats. Cleared from
    // _urlState so subsequent loadChats() (e.g. days dropdown change)
    // don't re-fire the original query.
    if (window._urlState && window._urlState.q) {
        const q = window._urlState.q;
        delete window._urlState.q;
        const input = document.getElementById('chats-global-search');
        if (input) input.value = q;
        chatsGlobalSearch();
    } else if (chatsState.searchActive && chatsState.searchQuery) {
        // A previously-active search is being re-run after a data
        // refresh (e.g. user changed the days dropdown).
        chatsGlobalSearch();
    } else {
        renderChatList();
    }

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

    // -- Pre-filters: project + Advanced pills -----------------------
    // These narrow the corpus BEFORE sort or search overlays. The same
    // filters apply in search-active mode (so a query operates only
    // on the user's chosen project / pill subset).
    var projectFilter = document.getElementById('chats-project-filter')?.value;
    if (projectFilter) {
        chats = chats.filter(function(c) { return c.project_name === projectFilter; });
    }
    if (chatsState.filters.has_commits) {
        chats = chats.filter(function(c) { return (c.commit_count || 0) > 0; });
    }
    if (chatsState.filters.has_unfinished) {
        chats = chats.filter(function(c) { return (c.unfinished_count || 0) > 0; });
    }

    // -- Search overlay ---------------------------------------------
    // When the search box has been submitted, intersect with the
    // IR-returned session set and replace the user's sort with
    // doc_score. The matched chunks-per-session map is consumed by
    // renderChatCard to render the in-card "Matches" footer.
    var searchHitsBySid = null;
    if (chatsState.searchActive) {
        var hitMap = {};
        chatsState.searchSessionsByScore.forEach(function(s) { hitMap[s.session_id] = s; });
        chats = chats.filter(function(c) { return hitMap[c.session_id] !== undefined; });
        chats.sort(function(a, b) {
            return (hitMap[b.session_id].doc_score || 0) - (hitMap[a.session_id].doc_score || 0);
        });
        searchHitsBySid = hitMap;
    } else {
        var sort = document.getElementById('chats-sort')?.value || 'recent';
        if (sort === 'longest') chats.sort(function(a, b) { return (b.message_count - a.message_count); });
        else if (sort === 'most-messages') chats.sort(function(a, b) { return (b.tool_count - a.tool_count); });
        else if (sort === 'most-commits') {
            chats.sort(function(a, b) {
                var ac = a.commit_count || 0;
                var bc = b.commit_count || 0;
                if (bc !== ac) return bc - ac;
                return (b.start_time || '').localeCompare(a.start_time || '');
            });
        }
        else if (sort === 'most-recent-commit') {
            chats.sort(function(a, b) {
                var at = a.latest_committed_at || '';
                var bt = b.latest_committed_at || '';
                if (at && !bt) return -1;
                if (bt && !at) return 1;
                if (at !== bt) return bt.localeCompare(at);
                return (b.start_time || '').localeCompare(a.start_time || '');
            });
        }
    }

    var filteredTotal = chats.length;
    var endIdx = Math.min(filteredTotal, (chatsState.page + 1) * chatsState.pageSize);
    var visible = chats.slice(0, endIdx);
    var hasMore = endIdx < filteredTotal;

    var searchSummary = '';
    if (chatsState.searchActive) {
        var irHitCount = chatsState.searchSessionsByScore.length;
        // IR returns hits across the FULL conversation index; the
        // listing is bounded by the current days window. When the IR
        // hit count exceeds what's visible, hint the user toward
        // widening the window so they don't think the engine missed
        // older matches.
        var hiddenByWindow = Math.max(0, irHitCount - filteredTotal);
        var hint = '';
        if (hiddenByWindow > 0) {
            hint = ' &middot; <span class="chats-search-hint">'
                + hiddenByWindow + ' more outside the current days window — '
                + '<a href="#" onclick="chatsExpandToAllTime();return false;">show All time</a>'
                + '</span>';
        }
        searchSummary = '<div class="chats-search-summary">'
            + '<span><strong>' + filteredTotal + '</strong> chat'
            + (filteredTotal === 1 ? '' : 's') + ' matching '
            + '<em>"' + escapeHtml(chatsState.searchQuery) + '"</em>'
            + ' &middot; sorted by relevance'
            + hint
            + '</span>'
            + '<button class="chats-clear-search-btn" onclick="chatsClearSearch()">Clear search</button>'
            + '</div>';
    }

    if (filteredTotal === 0) {
        container.innerHTML = searchSummary + renderEmptyChatListState();
        return;
    }

    // Date-grouped headers when sort=recent and not searching.
    // For all other sort modes (most-commits, longest, etc.) and for
    // search-active mode, group headers don't make sense — the cards
    // are ordered by something other than time.
    var sortValue = document.getElementById('chats-sort')?.value || 'recent';
    var groupByDate = !chatsState.searchActive && sortValue === 'recent';

    var listHTML;
    if (groupByDate) {
        var lastBucket = null;
        listHTML = visible.map(function(c) {
            var bucket = _chatsBucketLabel(c.start_time);
            var headerHTML = '';
            if (bucket !== lastBucket) {
                headerHTML = '<div class="chats-day-header">' + escapeHtml(bucket) + '</div>';
                lastBucket = bucket;
            }
            return headerHTML + renderChatCard(c, searchHitsBySid ? searchHitsBySid[c.session_id] : null);
        }).join('');
    } else {
        listHTML = visible.map(function(c) { return renderChatCard(c, searchHitsBySid ? searchHitsBySid[c.session_id] : null); }).join('');
    }

    container.innerHTML = searchSummary
        + listHTML
        + (hasMore ? renderLoadMore(filteredTotal - endIdx) : '');

    container.querySelectorAll('.chat-card').forEach(function(card) {
        card.addEventListener('click', function() { selectChat(card.dataset.sid); });
    });
    // Inner chunk rows are nested inside the card. Stop click
    // propagation so clicking a snippet jumps to that span instead of
    // selecting the whole chat at offset 0.
    container.querySelectorAll('.chat-card-chunk').forEach(function(el) {
        el.addEventListener('click', function(ev) {
            ev.stopPropagation();
            var sid = el.dataset.sid;
            var span = parseInt(el.dataset.span, 10);
            if (sid && !isNaN(span)) chatsJumpToHit(sid, span);
        });
    });
    // Clicking the project tag on a card sets the project filter to
    // that project — one-click "show me only this project". Stop
    // propagation so we don't ALSO open the chat.
    container.querySelectorAll('.chat-card-project[data-project]').forEach(function(el) {
        el.addEventListener('click', function(ev) {
            ev.stopPropagation();
            var sel = document.getElementById('chats-project-filter');
            if (!sel) return;
            sel.value = el.dataset.project;
            chatsProjectFilterChanged(el.dataset.project);
        });
    });
}

/** Card render — extracted so search-active mode can attach chunks. */
function renderChatCard(c, searchHit) {
    const ACTIVE_WINDOW_MS = 60 * 60 * 1000;
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
        + (c.project_name ? '<div class="chat-card-project" data-project="' + escapeHtml(c.project_name) + '"'
            + ' title="Filter listing to this project">' + escapeHtml(c.project_name) + '</div>' : '')
        + '<div class="chat-card-title">' + title + '</div>'
        + '<div class="chat-card-meta">'
        + '<span>' + activeDot + formatTimestamp(c.start_time) + '</span>'
        + '<span>' + (c.duration || '--') + '</span>'
        + '<span>' + c.message_count + ' msgs</span>'
        + '</div>'
        + renderChatBadges(c)
        + (searchHit ? renderChatChunks(c.session_id, searchHit.chunks || []) : '')
        + '</div>';
}

function renderChatChunks(sid, chunks) {
    if (!chunks || chunks.length === 0) return '';
    var top = chunks.slice(0, 3);
    var rest = chunks.length - top.length;
    var label = chunks.length === 1
        ? 'Match in 1 span:'
        : 'Matches in ' + chunks.length + ' spans:';
    return '<div class="chat-card-chunks">'
        + '<div class="chat-card-chunks-label">' + label + '</div>'
        + top.map(function(ch) {
            var snippet = _chatsRenderSnippet(ch.display_text || '');
            return '<div class="chat-card-chunk" data-sid="' + sid + '" data-span="' + ch.span_index + '"'
                + ' title="Jump to span ' + ch.span_index + ' in this conversation">'
                + '<span class="chunk-marker">›</span> '
                + '<span class="chunk-snippet">' + snippet + '</span>'
                + '</div>';
        }).join('')
        + (rest > 0 ? '<div class="chat-card-chunks-more">+' + rest + ' more match' + (rest === 1 ? '' : 'es') + '</div>' : '')
        + '</div>';
}

/**
 * Render a chunk snippet with the search query highlighted. Centers
 * the snippet around the FIRST query-token match so the user sees
 * useful context, not the always-truncated start of the span.
 *
 * Matching is per-token, case-insensitive, on word boundaries when
 * the token is alphanumeric (so "obs" matches "observability" but
 * doesn't try to match every "obs" inside punctuation noise).
 *
 * Returns escaped HTML with <mark> wrapping each match.
 */
function _chatsRenderSnippet(rawText) {
    var clean = cleanMsgText(rawText || '').trim();
    if (!clean) return '';

    var WIDTH = 200;
    var query = (chatsState.searchQuery || '').trim().replace(/\s*\(commit\)$/, '');
    // Tokenize the way the IR engine does: split on whitespace AND
    // common identifier separators (_, -, .). Otherwise a query like
    // "workflow_id" would never match the inline "workflow" inside a
    // chunk's text because there is no literal "workflow_id" token.
    var tokens = query
        ? query.split(/[\s_\-\.]+/).filter(function(t) { return t.length >= 2; })
        : [];

    if (tokens.length === 0) {
        return escapeHtml(clean.substring(0, WIDTH))
            + (clean.length > WIDTH ? '…' : '');
    }

    // Find the earliest match position across all tokens.
    var lower = clean.toLowerCase();
    var firstMatch = -1;
    for (var i = 0; i < tokens.length; i++) {
        var idx = lower.indexOf(tokens[i].toLowerCase());
        if (idx >= 0 && (firstMatch === -1 || idx < firstMatch)) firstMatch = idx;
    }

    // Center the snippet window around the first match. If no match
    // (semantic-only hit), fall through to the head of the text.
    var start = 0;
    if (firstMatch > WIDTH / 2) {
        start = Math.max(0, firstMatch - Math.floor(WIDTH / 2));
    }
    var end = Math.min(clean.length, start + WIDTH);
    var window = clean.substring(start, end);
    var prefix = start > 0 ? '…' : '';
    var suffix = end < clean.length ? '…' : '';

    // Build a single regex from tokens for one-pass replace; escape
    // user input so a stray ( or [ doesn't break the regex.
    var escTokens = tokens.map(function(t) {
        return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    });
    var pattern = new RegExp('(' + escTokens.join('|') + ')', 'gi');
    // escapeHtml first, THEN add <mark> tags. Otherwise the tags get
    // entity-encoded into &lt;mark&gt;.
    var escaped = escapeHtml(window);
    var highlighted = escaped.replace(pattern, '<mark>$1</mark>');
    return prefix + highlighted + suffix;
}

/**
 * Day-bucket label for the listing's section headers. Only used when
 * sort=recent (the default), so adjacent cards' buckets monotonically
 * decrease in recency.
 */
function _chatsBucketLabel(startTimeIso) {
    if (!startTimeIso) return 'Unknown';
    var t = new Date(startTimeIso);
    if (isNaN(t.getTime())) return 'Unknown';
    var now = new Date();
    var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var msPerDay = 86400000;
    var diffDays = Math.floor((startOfToday.getTime() - t.getTime()) / msPerDay);
    if (diffDays < 0) return 'Today';                 // future timestamps
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays <= 6) return diffDays + ' days ago';
    if (diffDays <= 13) return 'Last week';
    if (diffDays <= 29) return Math.floor(diffDays / 7) + ' weeks ago';
    if (diffDays <= 60) return 'Last month';
    if (diffDays <= 365) return Math.floor(diffDays / 30) + ' months ago';
    return 'Earlier';
}

function renderLoadMore(remaining) {
    return '<button class="chats-load-more-listing" onclick="chatsLoadMoreList()">'
        + 'Show more (' + remaining + ' remaining)'
        + '</button>';
}

function renderEmptyChatListState() {
    if (chatsState.searchActive) {
        return '<div class="empty-state">No matches for <em>"'
            + escapeHtml(chatsState.searchQuery) + '"</em> '
            + '<button class="chats-clear-search-btn" onclick="chatsClearSearch()">Clear search</button>'
            + '</div>';
    }
    if (chatsState.filters.has_commits || chatsState.filters.has_unfinished
        || document.getElementById('chats-project-filter')?.value) {
        return '<div class="empty-state">No chats match the current filters. '
            + '<button class="chats-clear-search-btn" onclick="chatsResetFiltersAll()">Reset filters</button>'
            + '</div>';
    }
    return '<div class="empty-state">No chats found</div>';
}

function chatsLoadMoreList() {
    chatsState.page = (chatsState.page || 0) + 1;
    renderChatList();
}

/**
 * Switch the listing's days window to "All time" so IR hits outside
 * the current window become visible. Triggered by the "more outside
 * the current days window" link in the search summary. Re-runs the
 * search after the data refresh so the same query rebuilds against
 * the full corpus.
 */
function chatsExpandToAllTime() {
    var sel = document.getElementById('chats-days');
    if (sel) sel.value = '0';
    // Preserve the active search so chatsState.searchActive +
    // searchQuery survive the loadChats. (loadChats will re-fire
    // the search automatically since searchActive is true.)
    loadChats();
    if (typeof _persistHash === 'function') _persistHash();
}

/** Reset listing position to first page. Call on any state change
 *  that affects what's visible. */
function chatsResetPage() { chatsState.page = 0; }

/** Hard reset: clear pills, project filter, search, page. Used by the
 *  empty-state Reset link when filters narrowed everything away. */
function chatsResetFiltersAll() {
    chatsState.filters.has_commits = false;
    chatsState.filters.has_unfinished = false;
    chatsUpdatePillVisuals();
    var projSel = document.getElementById('chats-project-filter');
    if (projSel) { projSel.value = ''; projSel.classList.remove('active'); }
    chatsClearSearch();  // also resets page + re-renders
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
        var commitsLabel = c.commit_count + ' commit' + (c.commit_count === 1 ? '' : 's');
        if (repoCount > 1) commitsLabel += ' across ' + repoCount + ' repos';
        parts.push('<span class="chat-card-badge">' + commitsLabel + '</span>');
    }
    if (c.unfinished_count > 0) {
        var unfinishedLabel = c.unfinished_count + ' left uncommitted';
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

    const [msgData, commitData, topicData, uncommittedData] = await Promise.all([
        fetchJSON('/api/chats/' + sessionId + '/messages?offset=0&limit=' + chatsState.limit),
        fetchJSON('/api/chats/' + sessionId + '/commits'),
        fetchJSON('/api/chats/' + sessionId + '/topics'),
        fetchJSON('/api/chats/' + sessionId + '/uncommitted-files'),
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

    // tldr line below the header chips. Hidden when no summary.
    renderChatTldr(topicData);

    // Topic-timeline rail to the left of the message stream. Hidden
    // when the session has no topic_summaries (LLM feature off, or
    // session not yet summarized).
    renderTopicRail(topicData);

    // Uncommitted-files banner above the message stream. Shows files
    // this session wrote that are STILL dirty in git RIGHT NOW —
    // distinct from the card badge (historical signal).
    renderUncommittedBanner(uncommittedData);

    renderMessages();
}

function renderChatTldr(topicData) {
    var existing = document.getElementById('chats-tldr');
    if (existing) existing.remove();
    if (!topicData || !topicData.tldr) return;
    var node = document.createElement('div');
    node.id = 'chats-tldr';
    node.className = 'chats-tldr-line';
    node.textContent = topicData.tldr;
    // Insert after the header.
    var header = document.getElementById('chats-viewer-header');
    if (header && header.parentNode) {
        header.parentNode.insertBefore(node, header.nextSibling);
    }
}

function renderTopicRail(topicData) {
    var existing = document.getElementById('chats-topic-rail');
    if (existing) existing.remove();
    if (!topicData || !topicData.topics || topicData.topics.length === 0) return;
    // Wrap the message stream and rail in a flex container so the
    // rail sits to the left and the messages flex-grow into the rest.
    var messagesEl = document.getElementById('chats-messages');
    if (!messagesEl || !messagesEl.parentNode) return;

    var rail = document.createElement('div');
    rail.id = 'chats-topic-rail';
    rail.className = 'chats-topic-rail';
    rail.innerHTML = '<div class="chats-topic-rail-title">Topics</div>'
        + topicData.topics.map(function(t, i) {
            var range = (t.turn_start != null && t.turn_end != null)
                ? ' <span class="topic-range">' + t.turn_start + '–' + t.turn_end + '</span>'
                : '';
            return '<div class="chats-topic-item" data-turn="' + (t.turn_start != null ? t.turn_start : '') + '"'
                + ' onclick="chatsJumpToTopic(' + (t.turn_start != null ? t.turn_start : 'null') + ')"'
                + ' title="' + escapeHtml(t.summary || '') + '">'
                + '<span class="topic-index">' + (i + 1) + '.</span> '
                + escapeHtml(t.title || '(untitled)')
                + range
                + '</div>';
        }).join('');

    // Re-wrap messages with the rail beside it. Look for an existing
    // wrapper to avoid double-nesting on subsequent selectChat calls.
    var wrapper = document.getElementById('chats-stream-wrapper');
    if (!wrapper) {
        wrapper = document.createElement('div');
        wrapper.id = 'chats-stream-wrapper';
        wrapper.className = 'chats-stream-wrapper';
        messagesEl.parentNode.insertBefore(wrapper, messagesEl);
        wrapper.appendChild(messagesEl);
    }
    wrapper.insertBefore(rail, messagesEl);
}

function chatsJumpToTopic(turnStart) {
    if (turnStart == null) return;
    // Re-load the message window centered on the topic's first turn,
    // mirroring chatsJumpToCommit's offset math.
    var contextWindow = Math.floor(chatsState.limit / 2);
    var offset = Math.max(0, turnStart - contextWindow);
    var sid = chatsState.selectedId;
    if (!sid) return;
    var url = '/api/chats/' + sid + '/messages?offset=' + offset + '&limit=' + chatsState.limit;
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Jumping to topic...</div>';
    fetchJSON(url).then(function(msgData) {
        if (msgData) {
            chatsState.messages = msgData.messages || [];
            chatsState.filteredCount = msgData.filtered_count || 0;
            chatsState.hasMore = msgData.has_more || false;
            chatsState.offset = chatsState.messages.length;
            chatsState._earliestLoaded = offset;
            renderMessages();
        }
    });
}

/**
 * Render the IR span-compatibility warning above the in-session
 * search hits. Inserts a brief notice when the inspector's
 * _check_span_compatibility surfaces drift between the cached span
 * map and the live IR document set (so the agent knows search results
 * may be approximate). No-op when the warning field is empty.
 */
function renderSpanCompatWarning(warning) {
    var existing = document.getElementById('chats-span-warning');
    if (existing) existing.remove();
    if (!warning) return;
    var node = document.createElement('div');
    node.id = 'chats-span-warning';
    node.className = 'chats-span-warning';
    node.textContent = '⚠ ' + warning;
    var hitsDiv = document.getElementById('chats-in-search-hits');
    if (hitsDiv && hitsDiv.parentNode) {
        hitsDiv.parentNode.insertBefore(node, hitsDiv);
    }
}

function renderUncommittedBanner(uncommittedData) {
    var existing = document.getElementById('chats-uncommitted-banner');
    if (existing) existing.remove();
    if (!uncommittedData || !uncommittedData.files || uncommittedData.files.length === 0) return;

    var banner = document.createElement('div');
    banner.id = 'chats-uncommitted-banner';
    banner.className = 'chats-uncommitted-banner';
    var fileList = uncommittedData.files.slice(0, 8).map(function(f) {
        var repoTag = f.repo ? '<span class="banner-repo">' + escapeHtml(f.repo) + '</span> ' : '';
        // Prefer repo-relative path for readability; full absolute
        // path goes into title (hover) for users who need it.
        var display = f.rel_path || f.path;
        return '<li title="' + escapeHtml(f.path) + '">' + repoTag
            + '<code>' + escapeHtml(display) + '</code></li>';
    }).join('');
    var more = uncommittedData.files.length > 8
        ? '<li style="color:var(--text-muted);">…and ' + (uncommittedData.files.length - 8) + ' more</li>'
        : '';
    banner.innerHTML = '<div class="banner-header">'
        + '⚠ ' + uncommittedData.count + ' file' + (uncommittedData.count === 1 ? '' : 's')
        + ' this session wrote are dirty in git right now'
        + '</div>'
        + '<ul class="banner-list">' + fileList + more + '</ul>';

    // Insert above the messages container.
    var anchor = document.getElementById('chats-messages');
    if (anchor && anchor.parentNode) {
        anchor.parentNode.insertBefore(banner, anchor);
    }
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

// Esc handler for the Chats tab.
// Priority order:
//   1. If the chat-detail viewer is open → close it.
//   2. Else if a search is active → clear it.
//   3. Otherwise no-op.
// Allowed inside the global-search input so users can abandon a query
// with one keypress; other inputs (in-chat search, etc.) keep their
// default Esc behavior so we don't fight focus-based UX.
document.addEventListener('keydown', function(ev) {
    if (ev.key !== 'Escape') return;
    var panel = document.getElementById('panel-chats');
    if (!panel || !panel.classList.contains('active')) return;

    var tag = (ev.target && ev.target.tagName) || '';
    var isInGlobalSearch = ev.target && ev.target.id === 'chats-global-search';
    if ((tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') && !isInGlobalSearch) return;

    var viewer = document.getElementById('chats-viewer');
    if (viewer && viewer.style.display !== 'none') {
        closeChat();
        return;
    }
    if (chatsState.searchActive) {
        chatsClearSearch();
        if (isInGlobalSearch) ev.target.blur();
    }
});

function renderChatHeader(meta) {
    if (!meta) return;
    var header = document.getElementById('chats-viewer-header');
    // Look up the cached listing-side entry for this session so the
    // header can carry commit_count / unfinished_count / tldr without
    // a second backend call. Falls back gracefully when the listing
    // hasn't loaded yet (e.g. deep-link straight to a chat).
    var listEntry = (chatsState.chats || []).find(function(c) {
        return c.session_id === meta.session_id;
    }) || {};

    var leftPieces = [
        '<code>' + (meta.session_id ? meta.session_id.substring(0, 8) : '--') + '</code>',
        (meta.message_count || 0) + ' msgs',
        meta.duration || '--',
    ];
    if (meta.start_time) leftPieces.push(formatTimestamp(meta.start_time));
    if (listEntry.engages_git && (listEntry.commit_count || 0) > 0) {
        var repos = listEntry.commits_by_repo ? Object.keys(listEntry.commits_by_repo).length : 0;
        var commitLabel = listEntry.commit_count + ' commits' + (repos > 1 ? ' / ' + repos + ' repos' : '');
        leftPieces.push(commitLabel);
    }
    if (listEntry.engages_git && (listEntry.unfinished_count || 0) > 0) {
        // Plain text, inherits the muted header color — same restraint
        // as the card badge. The warning glyph is intentionally absent
        // here too; the count itself is the signal.
        leftPieces.push(listEntry.unfinished_count + ' left uncommitted');
    }

    header.innerHTML = '<div class="chats-hdr-left">'
        + leftPieces.join(' &middot; ')
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

    // Project change resets page and re-renders. If a search is active,
    // re-run it so the project pre-filter ALSO restricts the search
    // corpus on the backend (project param goes alongside eligible_sids).
    chatsResetPage();
    if (chatsState.searchActive) {
        chatsGlobalSearch();
    } else {
        renderChatList();
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
        chatsClearSearch();
        return;
    }
    _lastGlobalQuery = q;

    var method = document.getElementById('chats-search-method').value;
    if (method === 'commit') return chatsCommitSearch(q);

    // Show a transient loading affordance in the listing while the IR
    // round-trip is in flight. Replaces the now-removed search-results
    // pane's loading message.
    var container = document.getElementById('chats-list');
    container.innerHTML = '<div class="loading">Searching for "' + escapeHtml(q) + '"...</div>';

    var project = document.getElementById('chats-project-filter').value;
    // eligible_sids carries the filter-pill outcome. If any pill is
    // active, send the session_ids the listing would currently show
    // so the IR engine pre-filters its scoring corpus instead of
    // ranking-then-filtering.
    var eligibleSids = chatsComputeEligibleSids();
    var url = '/api/chats/search?q=' + encodeURIComponent(q) + '&method=' + method
        + (project ? '&project=' + encodeURIComponent(project) : '')
        + (eligibleSids ? '&eligible_sids=' + encodeURIComponent(eligibleSids.join(',')) : '');
    var data = await fetchJSON(url);

    if (!data || data.error) {
        container.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Search failed')
            + ' <button class="chats-clear-search-btn" onclick="chatsClearSearch()">Clear search</button>'
            + '</div>';
        return;
    }

    // Populate search state. renderChatList consumes this to filter +
    // sort + attach chunks. No separate pane.
    chatsState.searchActive = true;
    chatsState.searchQuery = q;
    chatsState.searchSessionsByScore = data.sessions || [];
    chatsResetPage();
    renderChatList();
    // Persist q to URL hash so the search survives reload + is
    // shareable. _persistHash handles the encoding.
    if (typeof _persistHash === 'function') _persistHash();
}

/**
 * Clear the search state and re-render the listing as the unfiltered
 * (well, pill-and-project filtered) view it was before search.
 */
function chatsClearSearch() {
    chatsState.searchActive = false;
    chatsState.searchQuery = '';
    chatsState.searchSessionsByScore = [];
    chatsResetPage();
    var input = document.getElementById('chats-global-search');
    if (input) input.value = '';
    renderChatList();
    if (typeof _persistHash === 'function') _persistHash();
}

// Backward-compat shim: a few old call sites still call
// chatsCloseGlobalSearch when jumping into a chat from a hit. They
// should now just clear the search overlay before navigating.
function chatsCloseGlobalSearch() { chatsClearSearch(); }

async function chatsCommitSearch(q) {
    var container = document.getElementById('chats-list');
    container.innerHTML = '<div class="loading">Searching commits for "' + escapeHtml(q) + '"...</div>';

    var project = document.getElementById('chats-project-filter').value;
    var url = '/api/chats/search/commits?q=' + encodeURIComponent(q)
        + (project ? '&project=' + encodeURIComponent(project) : '');
    var data = await fetchJSON(url);

    if (!data || data.error) {
        container.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Commit search failed')
            + ' <button class="chats-clear-search-btn" onclick="chatsClearSearch()">Clear search</button>'
            + '</div>';
        return;
    }

    // Map commit-search results into the same {session_id, doc_score,
    // chunks} shape that keyword/semantic search uses, so renderChatList
    // can render both with one code path. Each commit becomes a "chunk"
    // whose snippet is "<hash> <message>" and whose span_index is the
    // turn at which the commit was issued (so click-to-jump works).
    var sessions = (data.sessions || []).map(function(sess) {
        return {
            session_id: sess.session_id,
            doc_score: sess.commits ? sess.commits.length : 0,
            chunks: (sess.commits || []).map(function(commit) {
                var snippet = (commit.hash || '') + '  ' + (commit.message || '');
                return {
                    span_index: commit.message_index != null ? commit.message_index : 0,
                    display_text: snippet,
                    score: 1,
                };
            }),
        };
    });

    chatsState.searchActive = true;
    chatsState.searchQuery = q + ' (commit)';
    chatsState.searchSessionsByScore = sessions;
    chatsResetPage();
    renderChatList();
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

    // Surface the span-compat warning if the IR engine flagged it
    // (PR #108 fix: the inspector now correctly reports
    // "Chunk mismatch" / "Session not in IR index" when the cached
    // span map drifts from the live IR document set).
    renderSpanCompatWarning(data.warning);

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

    // Determine the session's primary repo so cross-repo commits get
    // a "(<repo>) " prefix. The primary is the most-frequent repo in
    // the listing-side `commits_by_repo` for this session — same data
    // already cached in chatsState.chats. Falls back to no prefix
    // when repo info is unavailable.
    var listEntry = (chatsState.chats || []).find(function(c) {
        return c.session_id === chatsState.selectedId;
    }) || {};
    var primaryRepo = null;
    var byRepo = listEntry.commits_by_repo || {};
    Object.keys(byRepo).forEach(function(repo) {
        if (primaryRepo === null || byRepo[repo] > byRepo[primaryRepo]) {
            primaryRepo = repo;
        }
    });

    groups.forEach(function(g) {
        var clickable = g.message_index != null;
        var clickAttr = clickable
            ? ' onclick="chatsJumpToCommit(' + g.message_index + ')" title="Jump to this commit in the conversation"'
            : '';
        // Per-commit repo only ever set when session_commits.repo_name
        // is populated (currently NULL for all rows; reserved for a
        // follow-up that infers per-commit repo from the cwd or
        // ``git show``). When set and different from the session
        // primary, prefix the message with the repo name.
        var commitRepo = g.repo_name || '';
        var repoPrefix = '';
        if (commitRepo && commitRepo !== primaryRepo) {
            repoPrefix = '<span class="commit-repo-tag">' + escapeHtml(commitRepo) + '</span> ';
        }
        html += '<div class="chat-commit-marker' + (clickable ? ' clickable' : '') + '"' + clickAttr + '>'
            + '<code>' + g.hashes[0] + '</code> '
            + repoPrefix
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

// ---- Chats: Advanced expander, filter pills, eligible-sids ----

function chatsToggleAdvanced() {
    var panel = document.getElementById('chats-advanced');
    var btn = document.getElementById('chats-advanced-toggle');
    if (!panel || !btn) return;
    var isHidden = panel.style.display === 'none' || !panel.style.display;
    panel.style.display = isHidden ? 'block' : 'none';
    btn.classList.toggle('expanded', isHidden);
    btn.textContent = (isHidden ? 'Advanced ▴' : 'Advanced ▾');
}

function chatsToggleFilter(key) {
    if (!(key in chatsState.filters)) return;
    chatsState.filters[key] = !chatsState.filters[key];
    chatsUpdatePillVisuals();
    chatsResetPage();
    // If a global-search query is active, re-run it so the
    // eligible_sids set updates in lockstep. Otherwise just re-render
    // the listing-mode view.
    if (chatsState.searchActive) {
        chatsGlobalSearch();
    } else {
        renderChatList();
    }
}

function chatsResetFilters() {
    chatsState.filters.has_commits = false;
    chatsState.filters.has_unfinished = false;
    chatsUpdatePillVisuals();
    chatsResetPage();
    if (chatsState.searchActive) {
        chatsGlobalSearch();
    } else {
        renderChatList();
    }
}

function chatsUpdatePillVisuals() {
    var any = false;
    Object.keys(chatsState.filters).forEach(function(k) {
        var pill = document.getElementById('chats-pill-' + k.replace(/_/g, '-'));
        if (pill) pill.classList.toggle('active', !!chatsState.filters[k]);
        if (chatsState.filters[k]) any = true;
    });
    var reset = document.getElementById('chats-pill-reset');
    if (reset) reset.style.display = any ? '' : 'none';
}

/**
 * Compute the eligible session_ids list for search pre-filtering.
 * Returns null when no filter pills are active (caller should skip
 * the URL param so search ranges over everything).
 */
function chatsComputeEligibleSids() {
    var anyActive = Object.keys(chatsState.filters).some(function(k) { return chatsState.filters[k]; });
    if (!anyActive) return null;
    var chats = chatsState.chats || [];
    if (chatsState.filters.has_commits) {
        chats = chats.filter(function(c) { return (c.commit_count || 0) > 0; });
    }
    if (chatsState.filters.has_unfinished) {
        chats = chats.filter(function(c) { return (c.unfinished_count || 0) > 0; });
    }
    return chats.map(function(c) { return c.session_id; }).filter(Boolean);
}

/** Public alias for code that wants a single "redraw the list" call.
 *  Sort/project dropdown onchange handlers call this. Resets pagination
 *  to first page so a re-sort lands the user at the top. */
function applyChatsFiltersAndSort() {
    chatsResetPage();
    renderChatList();
}

// ---- Chats: Event listeners ----

document.getElementById('chats-sort')?.addEventListener('change', renderChatList);
document.getElementById('chats-days')?.addEventListener('change', function() {
    loadChats();
    if (typeof _persistHash === 'function') _persistHash();
});

// Debounced as-you-type search. Pressing Enter still fires immediately
// (Enter handler runs first). Empty-string keystrokes clear the active
// search instantly so the listing comes back without waiting for the
// debounce window. AbortController cancels in-flight IR requests when a
// new keystroke fires.
var _chatsSearchDebounce = null;
var _chatsInflightAbort = null;
const CHATS_SEARCH_DEBOUNCE_MS = 600;

function _chatsScheduleSearch() {
    if (_chatsSearchDebounce) clearTimeout(_chatsSearchDebounce);
    var input = document.getElementById('chats-global-search');
    var q = input ? input.value.trim() : '';
    if (!q) {
        // Empty input: clear immediately, no debounce. The listing
        // should snap back the moment the user blanks the field.
        if (chatsState.searchActive) chatsClearSearch();
        return;
    }
    _chatsSearchDebounce = setTimeout(function() {
        _chatsSearchDebounce = null;
        chatsGlobalSearch();
    }, CHATS_SEARCH_DEBOUNCE_MS);
}

document.getElementById('chats-global-search')?.addEventListener('input', _chatsScheduleSearch);

document.getElementById('chats-global-search')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        if (_chatsSearchDebounce) {
            clearTimeout(_chatsSearchDebounce);
            _chatsSearchDebounce = null;
        }
        chatsGlobalSearch();
    }
});

document.getElementById('chats-in-search-input')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') chatsInSessionSearch();
});

// Tab-wide keyboard shortcuts. Mimics the GitHub / Slack pattern:
// pressing "/" anywhere on the Chats tab focuses the search box.
document.addEventListener('keydown', function(ev) {
    if (ev.key !== '/') return;
    var panel = document.getElementById('panel-chats');
    if (!panel || !panel.classList.contains('active')) return;
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    var input = document.getElementById('chats-global-search');
    if (input) {
        ev.preventDefault();
        input.focus();
        input.select();
    }
});

// j / k / ArrowDown / ArrowUp / Enter — keyboard navigation through
// the visible chat cards. Active only on the Chats tab when no card
// is open and the user isn't typing in a form field. Enter opens the
// focused card.
chatsState.focusIndex = -1;

function _chatsVisibleCards() {
    return Array.from(document.querySelectorAll('#chats-list .chat-card'));
}

function _chatsSetFocus(idx) {
    var cards = _chatsVisibleCards();
    if (cards.length === 0) { chatsState.focusIndex = -1; return; }
    if (idx < 0) idx = 0;
    if (idx >= cards.length) idx = cards.length - 1;
    chatsState.focusIndex = idx;
    cards.forEach(function(c, i) { c.classList.toggle('focused', i === idx); });
    // Keep focus visible without yanking the page if the user just
    // clicked something else.
    var el = cards[idx];
    var rect = el.getBoundingClientRect();
    if (rect.top < 80 || rect.bottom > window.innerHeight) {
        el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
}

document.addEventListener('keydown', function(ev) {
    var panel = document.getElementById('panel-chats');
    if (!panel || !panel.classList.contains('active')) return;
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    // Suppressed when chat-detail viewer is open (the viewer owns its
    // own scroll/key behavior; nav between cards while reading a chat
    // would be jarring).
    var viewer = document.getElementById('chats-viewer');
    if (viewer && viewer.style.display !== 'none') return;

    if (ev.key === 'j' || ev.key === 'ArrowDown') {
        ev.preventDefault();
        _chatsSetFocus(chatsState.focusIndex + 1);
    } else if (ev.key === 'k' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        _chatsSetFocus(Math.max(0, chatsState.focusIndex - 1));
    } else if (ev.key === 'Enter' && chatsState.focusIndex >= 0) {
        var cards = _chatsVisibleCards();
        var card = cards[chatsState.focusIndex];
        if (card && card.dataset.sid) {
            ev.preventDefault();
            selectChat(card.dataset.sid);
        }
    }
});
"""
