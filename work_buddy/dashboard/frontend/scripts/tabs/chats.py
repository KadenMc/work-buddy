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
    // Stash of the user's sort dropdown value before a search took it
    // over. Restored to the dropdown on chatsClearSearch so users get
    // back to whatever they were sorting by before they searched.
    _priorSort: 'recent',
    // Pagination — numbered, single-page-at-a-time (matches the
    // costs > sessions UI via the shared wbRenderPager component).
    // 1-indexed; reset to 1 on any filter/search/sort change so the
    // user always lands at page 1.
    page: 1,
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
        // "Most Recent" = most recent ACTIVITY (last message timestamp,
        // i.e. end_time) — not chat-creation time. Falls back to
        // start_time when end_time is missing.
        if (sort === 'recent') {
            chats.sort(function(a, b) {
                var at = a.end_time || a.start_time || '';
                var bt = b.end_time || b.start_time || '';
                return bt.localeCompare(at);
            });
        }
        else if (sort === 'longest') chats.sort(function(a, b) { return (b.message_count - a.message_count); });
        else if (sort === 'most-messages') chats.sort(function(a, b) { return (b.tool_count - a.tool_count); });
        else if (sort === 'most-commits') {
            chats.sort(function(a, b) {
                var ac = a.commit_count || 0;
                var bc = b.commit_count || 0;
                if (bc !== ac) return bc - ac;
                return (b.end_time || b.start_time || '').localeCompare(a.end_time || a.start_time || '');
            });
        }
        else if (sort === 'most-recent-commit') {
            chats.sort(function(a, b) {
                var at = a.latest_committed_at || '';
                var bt = b.latest_committed_at || '';
                if (at && !bt) return -1;
                if (bt && !at) return 1;
                if (at !== bt) return bt.localeCompare(at);
                return (b.end_time || b.start_time || '').localeCompare(a.end_time || a.start_time || '');
            });
        }
    }

    // Numbered pagination — single page slice (NOT cumulative). Clamp
    // the current page in case filters dropped it past the new last
    // page.
    var filteredTotal = chats.length;
    var totalPages = Math.max(1, Math.ceil(filteredTotal / chatsState.pageSize));
    var page = Math.min(Math.max(chatsState.page || 1, 1), totalPages);
    chatsState.page = page;
    var startIdx = (page - 1) * chatsState.pageSize;
    var endIdx = Math.min(filteredTotal, page * chatsState.pageSize);
    var visible = chats.slice(startIdx, endIdx);

    var searchSummary = '';
    if (chatsState.searchActive) {
        var irHitCount = chatsState.searchSessionsByScore.length;
        // IR returns hits across the FULL conversation index; the
        // listing is bounded by (a) the current days window AND (b)
        // any active filter pills + project filter. When the IR hit
        // count exceeds what's visible, surface the gap so the user
        // doesn't think the engine missed matches.
        //
        // We can ONLY recommend "show All time" when the user isn't
        // already there. If days=0 the hidden hits are unreachable via
        // the days lever (they're filtered out by project / pill, or
        // they reference sessions that aren't surfaced by /api/chats at
        // all) — so we silently swallow the count rather than offering
        // a misleading "show All time" link that won't help.
        var daysVal = parseInt(document.getElementById('chats-days')?.value, 10);
        var onAllTime = (daysVal === 0);
        var hiddenByWindow = Math.max(0, irHitCount - filteredTotal);
        var hint = '';
        if (hiddenByWindow > 0 && !onAllTime) {
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
    // are ordered by something other than time. Buckets are keyed off
    // end_time (last activity) to match the sort key.
    var sortValue = document.getElementById('chats-sort')?.value || 'recent';
    var groupByDate = !chatsState.searchActive && sortValue === 'recent';

    var listHTML;
    if (groupByDate) {
        var lastBucket = null;
        listHTML = visible.map(function(c) {
            var bucket = _chatsBucketLabel(c.end_time || c.start_time);
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

    container.innerHTML = searchSummary + listHTML;

    // Numbered pager rendered into the #chats-pager div (sibling of
    // #chats-list under .chats-content). wbRenderPager auto-hides when
    // total <= pageSize, so single-page results stay clean.
    if (typeof wbRenderPager === 'function') {
        wbRenderPager('chats-pager', filteredTotal, page, chatsState.pageSize,
                      'chatsGoToPage');
    }

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
    // Clicking the session-id chip copies the full UUID to the
    // clipboard. Stop propagation so we don't ALSO open the chat
    // (and skip the default text-selection so the brief "Copied!"
    // flash on the chip is visible).
    container.querySelectorAll('.chat-card-sid').forEach(function(el) {
        el.addEventListener('click', function(ev) {
            ev.stopPropagation();
            _chatsCopySid(el);
        });
    });
}

/** Copy a session-id chip's text to the clipboard and flash a brief
 *  "Copied!" affordance on the chip itself. Uses navigator.clipboard
 *  when available with a deprecated execCommand fallback for older
 *  browsers / non-secure contexts. */
function _chatsCopySid(el) {
    var sid = el.textContent.trim();
    if (!sid) return;
    var done = function(ok) {
        if (!ok) return;
        var prior = el.textContent;
        el.classList.add('chat-card-sid--copied');
        el.textContent = 'Copied!';
        setTimeout(function() {
            el.classList.remove('chat-card-sid--copied');
            el.textContent = prior;
        }, 1100);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(sid).then(function() { done(true); })
            .catch(function() {
                // Permission denied or insecure context — fall through.
                done(_chatsLegacyCopy(sid));
            });
    } else {
        done(_chatsLegacyCopy(sid));
    }
}

/** Fallback clipboard copy for environments without the async API
 *  (older browsers, HTTP origins). Uses a hidden textarea +
 *  document.execCommand('copy'). Returns true on success. */
function _chatsLegacyCopy(text) {
    try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch (e) { return false; }
}

/** Card render — extracted so search-active mode can attach chunks. */
function renderChatCard(c, searchHit) {
    const ACTIVE_WINDOW_MS = 60 * 60 * 1000;
    const titleSrc = c.tldr || c.first_message || 'Untitled chat';
    const title = escapeHtml(cleanMsgText(titleSrc).trim());
    const lastTs = c.end_time || c.start_time || '';
    const startTs = c.start_time || '';
    const lastMs = lastTs ? new Date(lastTs).getTime() : NaN;
    const isActive = isFinite(lastMs) && (Date.now() - lastMs) < ACTIVE_WINDOW_MS;
    const activeDot = isActive
        ? '<span class="wb-active-dot" title="Active in the last hour"></span>'
        : '';

    // Both timestamps on every card. "Last" = most recent message
    // (end_time, also the sort key for Most Recent). "Started" = first
    // message (when the chat was created). When the chat has only one
    // turn end_time === start_time so we omit the duplicate.
    var sameTs = lastTs && startTs && lastTs === startTs;
    var timeMetaHTML = '';
    if (lastTs) {
        timeMetaHTML += '<span class="chat-card-time" title="Most recent message in this chat">'
            + activeDot
            + '<span class="chat-card-time-label">Last</span> '
            + escapeHtml(formatTimestamp(lastTs))
            + '</span>';
    }
    if (startTs && !sameTs) {
        timeMetaHTML += '<span class="chat-card-time" title="First message (when this chat was created)">'
            + '<span class="chat-card-time-label">Started</span> '
            + escapeHtml(formatTimestamp(startTs))
            + '</span>';
    }

    // Top-row header: project tag (left) + full session UUID (right).
    // Claude Code's --resume produces forked sessions with identical
    // first messages and start_times — without the session_id chip,
    // forks look like duplicates on the listing. The chip uses a
    // monospace ("mechanical") font so the UUID reads as a literal
    // identifier rather than prose.
    var projectHTML = c.project_name
        ? '<div class="chat-card-project" data-project="' + escapeHtml(c.project_name) + '"'
            + ' title="Filter listing to this project">' + escapeHtml(c.project_name) + '</div>'
        : '<div class="chat-card-project-placeholder"></div>';
    var sidHTML = c.session_id
        ? '<code class="chat-card-sid" title="Full Claude Code session ID — useful for distinguishing forks of the same conversation (--resume).">'
            + escapeHtml(c.session_id) + '</code>'
        : '';

    return '<div class="chat-card' + (c.session_id === chatsState.selectedId ? ' active' : '') + '"'
        + ' data-sid="' + c.session_id + '">'
        + '<div class="chat-card-header-row">'
        + projectHTML
        + sidHTML
        + '</div>'
        + '<div class="chat-card-title">' + title + '</div>'
        + '<div class="chat-card-meta">'
        + timeMetaHTML
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

/** Pager onClick handler — jumps to the given 1-indexed page and
 *  re-renders. Scrolls to the top of the listing so the user lands
 *  at the new page's first card. */
function chatsGoToPage(n) {
    chatsState.page = Math.max(1, n);
    renderChatList();
    var list = document.getElementById('chats-list');
    if (list && list.scrollIntoView) {
        list.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
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
 *  that affects what's visible. 1-indexed; "first page" is page 1. */
function chatsResetPage() { chatsState.page = 1; }

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
 * Render the activity badge row for a chat card: commits + unfinished
 * work (gated on git engagement), plus PRs and Tasks (independent of
 * git — a session can author a PR or be assigned a task without
 * committing). Empty cells render an em-dash, never a "0".
 */
function renderChatBadges(c) {
    var parts = [];

    // --- git-engagement badges (commits, unfinished) ---
    if (c.engages_git) {
        if (c.commit_count > 0) {
            var repoCount = c.commits_by_repo ? Object.keys(c.commits_by_repo).length : 0;
            var commitsLabel = c.commit_count + ' commit' + (c.commit_count === 1 ? '' : 's');
            if (repoCount > 1) commitsLabel += ' across ' + repoCount + ' repos';
            parts.push('<span class="chat-card-badge commits">' + commitsLabel + '</span>');
        }
        if (c.unfinished_count > 0) {
            var unfinishedLabel = c.unfinished_count + ' left uncommitted';
            parts.push('<span class="chat-card-badge unfinished" title="Files this session wrote that this session did not commit itself.">' + unfinishedLabel + '</span>');
        }
    }

    // --- PRs badge: "{authored} · {merged}" ---
    var prAuthored = c.pr_authored_count || 0;
    var prMerged = c.pr_merged_count || 0;
    var prsDetail = c.prs_detail || [];
    if (prAuthored > 0 || prMerged > 0) {
        var prTip = prsDetail.map(function(p) {
            return '↗ #' + p.pr_number + ' · ' + p.action
                + (p.ts ? ' · ' + formatTimestamp(p.ts) : '');
        }).join('\n');
        // Single PR → linkify the whole badge; otherwise the tooltip
        // lists each event (multiple PRs can't share one href).
        var prInner = 'PRs ' + prAuthored + ' · ' + prMerged;
        var prBadge;
        if (prsDetail.length === 1 && prsDetail[0].pr_url) {
            prBadge = '<a class="chat-card-badge prs" target="_blank" rel="noopener"'
                + ' href="' + escapeHtml(prsDetail[0].pr_url) + '"'
                + ' title="' + escapeHtml(prTip) + '">' + prInner + '</a>';
        } else {
            prBadge = '<span class="chat-card-badge prs" title="' + escapeHtml(prTip) + '">'
                + prInner + '</span>';
        }
        parts.push(prBadge);
    } else {
        parts.push('<span class="chat-card-badge badge-empty" title="No pull requests authored from this session">PRs —</span>');
    }

    // --- Tasks badge: count ---
    var taskCount = c.task_count || 0;
    var tasksDetail = c.tasks_detail || [];
    if (taskCount > 0) {
        var taskTip = tasksDetail.map(function(t) {
            var meta = [t.state, t.urgency].filter(Boolean).join(' · ');
            var text = (t.task_text || '').slice(0, 80);
            return '▫ ' + t.task_id + (meta ? ' · ' + meta : '')
                + (text ? ' — ' + text : '');
        }).join('\n');
        var taskLabel = taskCount + ' task' + (taskCount === 1 ? '' : 's');
        parts.push('<span class="chat-card-badge tasks" title="' + escapeHtml(taskTip) + '">'
            + taskLabel + '</span>');
    } else {
        parts.push('<span class="chat-card-badge badge-empty" title="No tasks assigned to this session">Tasks —</span>');
    }

    if (parts.length === 0) return '';
    return '<div class="chat-card-badges">' + parts.join('') + '</div>';
}

/**
 * Shared "enter viewer mode" helper. Every code path that opens the
 * chat-detail view MUST funnel through here so the toolbar-hiding
 * .chats-tab--viewer class is applied uniformly.
 *
 * Was a per-call-site duplication, which let the chunk-click and
 * commit-search jump paths silently bypass the class — they hid the
 * list element but left the toolbar + pager visible above the viewer.
 *
 * Pass opts.skipInSearchHide=true to preserve the in-chat search bar
 * (the jump paths immediately re-show it with the carried query).
 */
function _chatsEnterViewerMode(opts) {
    opts = opts || {};
    var panel = document.getElementById('panel-chats');
    if (panel) panel.classList.add('chats-tab--viewer');
    document.getElementById('chats-list').style.display = 'none';
    var viewer = document.getElementById('chats-viewer');
    viewer.style.display = 'flex';
    viewer.style.flexDirection = 'column';
    viewer.style.flex = '1';
    if (!opts.skipInSearchHide) {
        document.getElementById('chats-in-search').style.display = 'none';
    }
    document.getElementById('chats-commits-bar').style.display = 'none';
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

    // Single-pane swap: hide the listing + the cross-session toolbar
    // (search / filters / sort / Advanced), show the viewer at full
    // width. _chatsEnterViewerMode owns the class + display toggles
    // so jump-from-hit and jump-to-commit-search inherit the same
    // hide-toolbar behavior.
    _chatsEnterViewerMode();
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

    var _commits = (commitData && commitData.commits) || [];
    chatsState.commits = _commits;
    chatsState.topicData = topicData;
    chatsState.tasksData = null;     // lazy-loaded on first switch to Tasks
    chatsState._tasksLoading = false;
    chatsState.railPanel = null;     // reset active panel per session

    // tldr line below the header chips. Hidden when no summary.
    renderChatTldr(topicData);

    // Activity rail (Topics | Git | Tasks selector) to the left of the
    // message stream. Hosts topic summaries, git activity (commits + PRs),
    // and this session's task interactions. Hidden when the session has
    // none of the three.
    renderActivityRail();

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

// ---- Chats: activity rail (Topics | Git | Tasks) ----
// The left rail hosts three switchable panels. Per-stream colors live in
// the panel CONTENT (commits green, PRs purple, tasks orange — via the
// .pr-marker / .task-marker classes); the selector pills stay neutral.

function _railTopicsHtml(topicData) {
    if (!topicData || !topicData.topics || topicData.topics.length === 0) {
        return '<div class="chats-rail-empty">No topic summary for this session.</div>';
    }
    return topicData.topics.map(function(t, i) {
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
}

function _railGitHtml(commits) {
    commits = commits || [];
    var listEntry = (chatsState.chats || []).find(function(c) {
        return c.session_id === chatsState.selectedId;
    }) || {};
    var prs = listEntry.prs_detail || [];
    if (commits.length === 0 && prs.length === 0) {
        return '<div class="chats-rail-empty">No commits or PRs from this session.</div>';
    }

    // Group commits by message to dedupe retried/amended commits.
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
                timestamp: c.timestamp || '', repo_name: c.repo_name || '',
            };
            seen[key] = g;
            groups.push(g);
        }
    });
    groups.sort(function(a, b) { return (a.timestamp || '').localeCompare(b.timestamp || ''); });

    var html = '';
    if (commits.length > 0) {
        html += '<div class="chats-rail-section-title">'
            + groups.length + ' commit' + (groups.length !== 1 ? 's' : '')
            + (commits.length !== groups.length ? ' (' + commits.length + ' incl. retries)' : '')
            + '</div>';
        var primaryRepo = null;
        var byRepo = listEntry.commits_by_repo || {};
        Object.keys(byRepo).forEach(function(repo) {
            if (primaryRepo === null || byRepo[repo] > byRepo[primaryRepo]) primaryRepo = repo;
        });
        groups.forEach(function(g) {
            var clickable = g.message_index != null;
            var clickAttr = clickable
                ? ' onclick="chatsJumpToCommit(' + g.message_index + ')" title="Jump to this commit in the conversation"'
                : '';
            var commitRepo = g.repo_name || '';
            var repoPrefix = (commitRepo && commitRepo !== primaryRepo)
                ? '<span class="commit-repo-tag">' + escapeHtml(commitRepo) + '</span> ' : '';
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
    }
    if (prs.length > 0) {
        html += '<div class="chats-rail-section-title">Pull requests (' + prs.length + ')</div>';
        var prsSorted = prs.slice().sort(function(a, b) {
            return (a.ts || '').localeCompare(b.ts || '');
        });
        prsSorted.forEach(function(p) {
            var when = p.ts ? formatTimestamp(p.ts) : '';
            var title = p.title || '';
            var state = (p.state || p.action || '').toString().toLowerCase();
            var stateClass = state.replace(/[^a-z]/g, '');
            html += '<div class="chat-commit-marker pr-marker">'
                + '<a href="' + escapeHtml(p.pr_url) + '" target="_blank" rel="noopener"'
                + ' class="commit-msg pr-num" title="Open PR on GitHub">↗ #' + p.pr_number + '</a> '
                + (title ? '<span class="commit-msg pr-title">' + escapeHtml(title) + '</span>' : '')
                + '<span class="commit-meta">'
                + (state ? '<span class="pr-state pr-state-' + stateClass + '">' + escapeHtml(state) + '</span>' : '')
                + (when ? '<span>' + when + '</span>' : '')
                + '</span>'
                + '</div>';
        });
    }
    return html;
}

function _railTasksHtml(tasksData) {
    if (tasksData == null) {
        return '<div class="chats-rail-empty">Loading tasks…</div>';
    }
    var tasks = (tasksData && tasksData.tasks) || [];
    if (tasks.length === 0) {
        return '<div class="chats-rail-empty">No task interactions in this session.</div>';
    }
    return tasks.map(function(t) {
        var roleBadges = (t.roles || []).map(function(r) {
            return '<span class="task-role">' + escapeHtml(r) + '</span>';
        }).join('');
        var state = t.state
            ? '<span class="task-state">' + escapeHtml(t.state) + '</span>' : '';
        var text = t.task_text
            ? '<div class="task-text">' + escapeHtml(t.task_text) + '</div>' : '';
        return '<div class="chat-commit-marker task-marker">'
            + '<div class="task-head">'
            +   '<code class="task-id">' + escapeHtml(t.task_id) + '</code>'
            +   state
            + '</div>'
            + (roleBadges ? '<div class="task-roles-row">' + roleBadges + '</div>' : '')
            + text
            + '</div>';
    }).join('');
}

function _loadRailTasks() {
    var sid = chatsState.selectedId;
    if (!sid || chatsState._tasksLoading) return;
    chatsState._tasksLoading = true;
    fetchJSON('/api/chats/' + sid + '/tasks').then(function(data) {
        chatsState._tasksLoading = false;
        chatsState.tasksData = data || { tasks: [] };
        var panel = document.getElementById('chats-rail-tasks');
        if (panel) panel.innerHTML = _railTasksHtml(chatsState.tasksData);
    });
}

function chatsRailSwitch(panelId) {
    var rail = document.getElementById('chats-topic-rail');
    if (!rail) return;
    var btn = rail.querySelector('.chats-rail-pill[data-panel="' + panelId + '"]');
    if (btn && btn.classList.contains('disabled')) return;  // grayed/empty tab
    chatsState.railPanel = panelId;
    rail.querySelectorAll('.chats-rail-pill').forEach(function(b) {
        b.classList.toggle('active', b.dataset.panel === panelId);
    });
    rail.querySelectorAll('.chats-rail-panel').forEach(function(p) {
        p.classList.toggle('active', p.id === 'chats-rail-' + panelId);
    });
    if (panelId === 'tasks' && chatsState.tasksData == null) _loadRailTasks();
}

function renderActivityRail() {
    var existing = document.getElementById('chats-topic-rail');
    if (existing) existing.remove();
    // The horizontal commits bar is superseded by the rail's Git panel.
    var oldBar = document.getElementById('chats-commits-bar');
    if (oldBar) oldBar.style.display = 'none';

    var messagesEl = document.getElementById('chats-messages');
    if (!messagesEl || !messagesEl.parentNode) return;

    var topicData = chatsState.topicData;
    var commits = chatsState.commits || [];
    var listEntry = (chatsState.chats || []).find(function(c) {
        return c.session_id === chatsState.selectedId;
    }) || {};
    var prs = listEntry.prs_detail || [];

    // The rail is PERMANENT for any selected chat. A panel with no content
    // doesn't hide the rail — it just disables (grays) its own tab. This is
    // both the intended UX and what keeps the rail from vanishing when you
    // page through chats that happen to have, say, no topics.
    var enabled = {
        topics: !!(topicData && topicData.topics && topicData.topics.length),
        git: commits.length > 0 || prs.length > 0,
        // Tasks is lazy: enable on any cheap "might be non-empty" signal —
        // an assigned-task hint, commits (which may reference tasks →
        // developed), or an already-loaded non-empty result.
        tasks: (listEntry.tasks_detail || []).length > 0 || commits.length > 0
            || !!(chatsState.tasksData && (chatsState.tasksData.tasks || []).length > 0),
    };

    // Keep the user's current tab if it's still enabled; else first enabled;
    // else Topics (the rail still shows, with every tab grayed).
    var order = ['topics', 'git', 'tasks'];
    var active = chatsState.railPanel;
    if (!active || !enabled[active]) {
        active = order.filter(function(id) { return enabled[id]; })[0] || 'topics';
    }
    chatsState.railPanel = active;

    function pill(id, label) {
        var off = !enabled[id];
        return '<button class="costs-pill chats-rail-pill'
            + (active === id ? ' active' : '') + (off ? ' disabled' : '') + '"'
            + ' data-panel="' + id + '"'
            + (off ? ' disabled title="Nothing to show"' : ' onclick="chatsRailSwitch(\'' + id + '\')"')
            + '>' + label + '</button>';
    }
    function panel(id, html) {
        return '<div class="chats-rail-panel' + (active === id ? ' active' : '') + '" id="chats-rail-' + id + '">'
            + html + '</div>';
    }

    var rail = document.createElement('div');
    rail.id = 'chats-topic-rail';
    rail.className = 'chats-topic-rail';
    rail.innerHTML =
        '<div class="chats-rail-selector">'
        + pill('topics', 'Topics') + pill('git', 'Git') + pill('tasks', 'Tasks')
        + '</div>'
        + panel('topics', _railTopicsHtml(topicData))
        + panel('git', _railGitHtml(commits))
        + panel('tasks', _railTasksHtml(chatsState.tasksData));

    // Wrap the message stream + rail in a flex row. Hardened: always ensure
    // #chats-messages lives inside the wrapper before inserting the rail —
    // otherwise a stale wrapper (messages moved out) made insertBefore throw,
    // which dropped the rail and kept it gone until a full reload.
    var wrapper = document.getElementById('chats-stream-wrapper');
    if (!wrapper) {
        wrapper = document.createElement('div');
        wrapper.id = 'chats-stream-wrapper';
        wrapper.className = 'chats-stream-wrapper';
        messagesEl.parentNode.insertBefore(wrapper, messagesEl);
    }
    if (messagesEl.parentNode !== wrapper) wrapper.appendChild(messagesEl);
    wrapper.insertBefore(rail, messagesEl);

    // If we opened straight onto Tasks, kick off its lazy fetch.
    if (active === 'tasks' && chatsState.tasksData == null) _loadRailTasks();
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
    // Swap views back: hide the chat detail, show the listing, and
    // remove the viewer-mode class so the cross-session toolbar
    // (search/filters/sort/Advanced) becomes visible again.
    var panel = document.getElementById('panel-chats');
    if (panel) panel.classList.remove('chats-tab--viewer');
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

    // If Commit method is selected but the project just got cleared,
    // fall back to Hybrid (commit search needs a project). Do this
    // BEFORE the search/render so the method swap is reflected in
    // whatever re-fire happens next.
    var methodSelect = document.getElementById('chats-search-method');
    if (methodSelect.value === 'commit' && !project) {
        methodSelect.value = 'keyword,semantic';
        chatsSearchMethodChanged('keyword,semantic');
    }

    // Project change resets pagination + re-renders. If a search is
    // active, re-fire it so the project pre-filter narrows the
    // search corpus on the backend (project goes alongside the
    // eligible_sids set). Either branch fully owns the re-render;
    // there used to be a third unconditional renderChatList() here
    // which raced with chatsGlobalSearch's loading state and
    // double-painted the list.
    chatsResetPage();
    if (chatsState.searchActive) {
        chatsGlobalSearch();
    } else {
        renderChatList();
    }
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

    // If a search is currently active, re-fire it under the new
    // method so the user sees an immediate update instead of having
    // to retype the query. Used to be silently no-op — confusing.
    if (chatsState.searchActive && chatsState.searchQuery) {
        chatsGlobalSearch();
    }
}

async function chatsGlobalSearch() {
    var q = document.getElementById('chats-global-search').value.trim();
    if (!q) {
        // Empty input: abort any pending request and clear search state.
        _chatsAbortInflight();
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

    // Cancel any in-flight prior request, then start a new one with
    // its own AbortController. The "stale completion" guard below
    // (`_chatsInflightAbort !== controller`) is the belt to abort's
    // suspenders — if abort raced and our awaited promise still
    // resolved, we still won't trample state for a query the user
    // already superseded.
    _chatsAbortInflight();
    var controller = new AbortController();
    _chatsInflightAbort = controller;

    var data;
    try {
        var resp = await fetch(url, { signal: controller.signal });
        data = await resp.json();
    } catch (err) {
        if (err && err.name === 'AbortError') {
            // Deliberately cancelled by a follow-on keystroke; don't
            // touch the listing — the caller that aborted us will
            // render whatever it wants.
            return;
        }
        data = null;
    }

    // Stale-completion guard: if a newer query has taken over the
    // controller slot, bail without rendering. Saves us from races
    // where the network finished before AbortController could.
    if (_chatsInflightAbort !== controller) return;
    _chatsInflightAbort = null;

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
    // Lock the sort dropdown to "Most Relevant" so the user can see
    // (instead of having to remember) that doc_score is what's
    // actually ordering the cards. Restored on chatsClearSearch.
    _chatsLockSortToRelevance();
    renderChatList();
    // Persist q to URL hash so the search survives reload + is
    // shareable. _persistHash handles the encoding.
    if (typeof _persistHash === 'function') _persistHash();
}

/** Lock the sort dropdown to "Most Relevant" while a search is
 *  active. Stashes the user's prior selection so chatsClearSearch
 *  can restore it. Idempotent — safe to call repeatedly (the
 *  prior-sort stash only updates when the dropdown isn't already
 *  on "relevance"). */
function _chatsLockSortToRelevance() {
    var sel = document.getElementById('chats-sort');
    if (!sel) return;
    if (sel.value !== 'relevance') {
        chatsState._priorSort = sel.value;
    }
    // Unhide the option (it's `hidden` by default in the HTML so it
    // doesn't appear in the user's everyday menu) so the dropdown
    // can actually display it as the selected value.
    var opt = sel.querySelector('option[value="relevance"]');
    if (opt) opt.hidden = false;
    sel.value = 'relevance';
    sel.disabled = true;
    sel.classList.add('chats-sort--locked');
}

/** Restore the dropdown to the user's prior sort and unlock it.
 *  Called when the search clears. */
function _chatsUnlockSort() {
    var sel = document.getElementById('chats-sort');
    if (!sel) return;
    sel.disabled = false;
    sel.classList.remove('chats-sort--locked');
    if (sel.value === 'relevance') {
        sel.value = chatsState._priorSort || 'recent';
    }
    // Re-hide the "Most Relevant" option so it doesn't appear in
    // the dropdown menu while the user is browsing in non-search
    // mode.
    var opt = sel.querySelector('option[value="relevance"]');
    if (opt) opt.hidden = true;
}

/** Abort any in-flight search fetch. Idempotent — safe to call
 *  even when there's nothing in flight. Called from: every new
 *  search dispatch, empty-input clears, explicit search clears,
 *  and the debounce scheduler when it sees an empty value. */
function _chatsAbortInflight() {
    if (_chatsInflightAbort) {
        try { _chatsInflightAbort.abort(); } catch (e) { /* ignore */ }
        _chatsInflightAbort = null;
    }
}

/**
 * Clear the search state and re-render the listing as the unfiltered
 * (well, pill-and-project filtered) view it was before search.
 */
function chatsClearSearch() {
    // Cancel any pending in-flight search before we drop state; a
    // resolution arriving after this point would otherwise try to
    // re-activate searchActive against a now-cleared query.
    _chatsAbortInflight();
    chatsState.searchActive = false;
    chatsState.searchQuery = '';
    chatsState.searchSessionsByScore = [];
    chatsResetPage();
    // Restore the sort dropdown to whatever the user had selected
    // before the search took it over.
    _chatsUnlockSort();
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

    // Route through the shared helper so the .chats-tab--viewer class
    // is applied — see _chatsEnterViewerMode for the rationale.
    _chatsEnterViewerMode();
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
        var _commits = (commitData && commitData.commits) || [];
        chatsState.commits = _commits;
        renderCommitsBar(_commits);
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

    // Route through the shared helper so the .chats-tab--viewer class
    // is applied — without it the cross-session toolbar + pager would
    // stay visible on top of the chat-detail view. Pass
    // skipInSearchHide because we're about to re-open the in-chat
    // search bar with the carried query a few lines below.
    _chatsEnterViewerMode({ skipInSearchHide: true });
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
        var _commits = (commitData && commitData.commits) || [];
        chatsState.commits = _commits;
        renderCommitsBar(_commits);
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
    // Superseded by the activity rail's Git panel. Kept as the entry point
    // the jump handlers (chatsJumpToCommitSearch / chatsJumpToHit) already
    // call: store the commits and rebuild the rail, which renders commits +
    // PRs in its Git panel and hides the old horizontal #chats-commits-bar.
    chatsState.commits = commits || [];
    renderActivityRail();
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

// Debounced as-you-type search. Pressing Enter or clicking the
// explicit "Search" button still fires immediately (Enter handler /
// button onclick runs first and cancels the pending debounce).
// Empty-string keystrokes clear the active search instantly so the
// listing comes back without waiting for the debounce window.
// AbortController cancels in-flight IR requests when a new keystroke
// fires.
//
// 1500ms was chosen after the user noted 600ms felt too eager — long
// enough that finger-pause-during-typing doesn't fire a stale prefix,
// short enough that a deliberate pause does still trigger auto-search.
// Power users who want immediate firing have the Enter key and the
// Search button.
var _chatsSearchDebounce = null;
var _chatsInflightAbort = null;
const CHATS_SEARCH_DEBOUNCE_MS = 1500;

function _chatsScheduleSearch() {
    if (_chatsSearchDebounce) clearTimeout(_chatsSearchDebounce);
    // Every keystroke kills any in-flight search. This is the key
    // resource-protection invariant the user asked for: if the
    // debounce fired and a request was already on the wire when the
    // user resumed typing, the request gets cancelled before its
    // (now-stale) results can come back and overwrite the listing.
    _chatsAbortInflight();
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

    // Inverted-vim binding per the user's preference recorded in
    // CLAUDE.local.md (`feedback_keyboard_inverted_jk`): j moves UP,
    // k moves DOWN. The arrow keys keep their literal direction so
    // muscle-memory still works for users who prefer them.
    if (ev.key === 'k' || ev.key === 'ArrowDown') {
        ev.preventDefault();
        _chatsSetFocus(chatsState.focusIndex + 1);
    } else if (ev.key === 'j' || ev.key === 'ArrowUp') {
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
