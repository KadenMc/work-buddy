"""Pin the search-merge invariants of the Chats dashboard tab.

These are JS-string and HTML-string assertions over the rendered
dashboard surface. They catch regressions where someone accidentally
re-introduces a separate search-results pane, removes the listing
pagination, or breaks the contract the legacy commit-search shim
depends on.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def chats_js() -> str:
    from work_buddy.dashboard.frontend.scripts.tabs import chats

    return chats.script()


@pytest.fixture(scope="module")
def panel_html() -> str:
    """Return the rendered Chats-tab HTML.

    The dashboard composes one big string via the private ``_html()``
    helper; we call it directly so test assertions can scan the
    Chats panel without spinning up the Flask app.
    """
    from work_buddy.dashboard.frontend import html as html_mod

    return html_mod._html()


@pytest.fixture(scope="module")
def styles_text() -> str:
    from work_buddy.dashboard.frontend import styles

    if hasattr(styles, "styles"):
        return styles.styles()
    if hasattr(styles, "STYLES"):
        return styles.STYLES
    if hasattr(styles, "_styles"):
        return styles._styles()
    return ""


# ---------------------------------------------------------------------------
# Topic rail: title and turn range occupy stable, non-overlapping columns
# ---------------------------------------------------------------------------


def test_topic_rail_reserves_space_for_centered_turn_range(
    chats_js: str, styles_text: str,
) -> None:
    assert '<span class="topic-title">' in chats_js

    item_rule = re.search(r"\.chats-topic-item\s*\{([^}]+)\}", styles_text)
    assert item_rule is not None
    assert "display: grid" in item_rule.group(1)
    assert "grid-template-columns: auto minmax(0, 1fr) auto" in item_rule.group(1)
    assert "align-items: center" in item_rule.group(1)

    title_rule = re.search(
        r"\.chats-topic-item \.topic-title\s*\{([^}]+)\}", styles_text,
    )
    assert title_rule is not None
    assert "min-width: 0" in title_rule.group(1)
    assert "overflow-wrap: anywhere" in title_rule.group(1)

    range_rule = re.search(
        r"\.chats-topic-item \.topic-range\s*\{([^}]+)\}", styles_text,
    )
    assert range_rule is not None
    assert "white-space: nowrap" in range_rule.group(1)
    assert "float:" not in range_rule.group(1)


# ---------------------------------------------------------------------------
# Search-merge: there is exactly one renderer; no separate results pane
# ---------------------------------------------------------------------------


def test_separate_search_results_pane_is_gone(panel_html: str) -> None:
    """The `#chats-search-results` element must NOT exist in the HTML.
    Search results render INTO `#chats-list` via renderChatList.
    """
    assert 'id="chats-search-results"' not in panel_html
    assert "chats-search-results" not in panel_html


def test_renderchatlist_handles_search_active_branch(chats_js: str) -> None:
    """renderChatList must own both listing and search-active modes."""
    # The search overlay branch is the load-bearing piece of the merge.
    assert "chatsState.searchActive" in chats_js
    assert "searchSessionsByScore" in chats_js
    assert "doc_score" in chats_js
    # And there must NOT be any leftover code that builds a second
    # render path keyed off chats-search-results.
    assert "chats-search-results" not in chats_js


def test_chats_global_search_populates_state_not_a_separate_pane(
    chats_js: str,
) -> None:
    """The search handler should set searchActive + searchSessionsByScore,
    not write to a separate DOM pane.
    """
    # Find the chatsGlobalSearch function body.
    m = re.search(
        r"async function chatsGlobalSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "chatsGlobalSearch() not found"
    body = m.group(1)
    assert "chatsState.searchActive = true" in body
    assert "chatsState.searchSessionsByScore" in body
    assert "renderChatList()" in body
    # Anti-pattern: writing to a separate results div.
    assert "chats-search-results" not in body


def test_commit_search_uses_same_render_path(chats_js: str) -> None:
    """chatsCommitSearch must produce the merged-search shape, not its
    own pane render. Each commit becomes a chunk with span_index =
    message_index so clicking jumps to the same place.
    """
    m = re.search(
        r"async function chatsCommitSearch\(q\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "chatsCommitSearch() not found"
    body = m.group(1)
    assert "chatsState.searchActive = true" in body
    assert "chatsState.searchSessionsByScore" in body
    assert "renderChatList()" in body
    assert "chats-search-results" not in body
    # And the per-commit → chunk mapping uses message_index for the
    # span_index field so the existing chatsJumpToHit click-handler
    # works without modification.
    assert "message_index" in body
    assert "span_index" in body


# ---------------------------------------------------------------------------
# Pagination: numbered pages (shared wbRenderPager) + reset on state changes
# ---------------------------------------------------------------------------


def test_listing_has_pagination_state(chats_js: str) -> None:
    assert "chatsState.page" in chats_js
    assert "chatsState.pageSize" in chats_js
    # 1-indexed numbered pager (matches costs > sessions). The
    # cumulative "Load more" handler is gone.
    assert "chatsGoToPage" in chats_js
    assert "chatsResetPage" in chats_js
    assert "chatsLoadMoreList" not in chats_js, (
        "Numbered pagination supersedes the cumulative 'Load more' button"
    )


def test_listing_renders_via_shared_pager(chats_js: str, panel_html: str) -> None:
    """The listing must mount the shared wbRenderPager component (used
    by costs > sessions and threads) into a sibling #chats-pager div,
    instead of appending an inline 'Show more' button at the end of
    the list.
    """
    assert "wbRenderPager(" in chats_js
    # The mount-point lives in the HTML scaffold so the listing
    # renderer doesn't have to construct it dynamically.
    assert 'id="chats-pager"' in panel_html
    # The old in-list 'Show more' affordance must be gone — both the
    # renderer and the CSS class.
    assert "Show more" not in chats_js
    assert "chats-load-more-listing" not in chats_js


def test_filter_changes_reset_pagination(chats_js: str) -> None:
    """Toggling a pill, a project, a sort, or the days window must
    reset chatsState.page to the first page. Otherwise users on page 5
    would see a confusing partial slice after re-filtering.
    """
    # Direct calls to chatsResetPage in the relevant handlers. The advanced
    # filter toggle flows through the shared widget's onChange adapter.
    for handler in (
        "_chatsOnAdvChange",
        "chatsResetFilters",
        "chatsProjectFilterChanged",
        "applyChatsFiltersAndSort",
        "loadChats",
    ):
        m = re.search(
            r"(async\s+)?function " + re.escape(handler) + r"\(.*?\)\s*\{(.*?)^\}",
            chats_js,
            re.DOTALL | re.MULTILINE,
        )
        assert m is not None, f"{handler}() not found"
        body = m.group(2)
        assert "chatsResetPage" in body, (
            f"{handler}() must call chatsResetPage to reset pagination"
        )


# ---------------------------------------------------------------------------
# Toolbar layout: project NOT in Advanced; "All time" option present
# ---------------------------------------------------------------------------


def test_project_filter_lives_in_main_toolbar(panel_html: str) -> None:
    """The project select must be a sibling of the search input in the
    main `.chats-toolbar`, NOT nested inside `#chats-advanced`.
    """
    # The project select element must appear BEFORE the advanced panel
    # in HTML source order — a cheap proxy for "in the toolbar."
    proj_idx = panel_html.find('id="chats-project-filter"')
    advanced_idx = panel_html.find('id="chats-advanced"')
    assert proj_idx > 0, "project-filter element missing"
    assert advanced_idx > 0, "advanced panel missing"
    assert proj_idx < advanced_idx, (
        "project filter must appear before #chats-advanced — currently "
        f"proj={proj_idx} advanced={advanced_idx}"
    )


def test_days_dropdown_has_all_time_and_defaults_30(panel_html: str) -> None:
    assert '<option value="30" selected>30 days</option>' in panel_html
    assert '<option value="0">All time</option>' in panel_html


# ---------------------------------------------------------------------------
# Visual restraint: no loud emojis or accent colors on the badge row
# ---------------------------------------------------------------------------


def test_card_badge_has_no_loud_emoji_or_accent_color(
    chats_js: str, styles_text: str,
) -> None:
    """The chat-card badges should be quiet: plain text, muted color.
    No 🌿, no ⚠, no var(--accent) on the badge text.
    """
    # Locate the badge renderer.
    m = re.search(
        r"function renderChatBadges\(c\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    assert "🌿" not in body, "branch emoji should be dropped from card badge"
    # Header header-string still ok to use ⚠ in the BANNER (different
    # context), but NOT inside the per-card badge row.
    assert "⚠" not in body, "warning glyph should be dropped from card badge"
    # CSS: unfinished badge must NOT use accent color anymore.
    if styles_text:
        m2 = re.search(
            r"\.chat-card-badge\.unfinished\s*\{([^}]+)\}",
            styles_text,
        )
        if m2:
            assert "var(--accent)" not in m2.group(1), (
                "unfinished badge should not use loud accent color"
            )


# ---------------------------------------------------------------------------
# Esc clears active search (in addition to closing the chat detail)
# ---------------------------------------------------------------------------


def test_esc_clears_active_search(chats_js: str) -> None:
    """Document keydown handler must call chatsClearSearch when Esc
    fires while no chat-detail viewer is open and a search is active.
    """
    assert "chatsClearSearch" in chats_js
    # Locate the keydown handler for the chats tab.
    m = re.search(
        r"document\.addEventListener\('keydown', function\(ev\)\s*\{(.*?)\}\);",
        chats_js,
        re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    assert "Escape" in body
    assert "chatsClearSearch()" in body
    assert "closeChat()" in body  # detail-view close path stays intact


# ---------------------------------------------------------------------------
# Polish: debounced search, "/" focus, j/k nav, URL hash, highlighting
# ---------------------------------------------------------------------------


def test_debounced_search_wires_input_event(chats_js: str) -> None:
    """As-you-type debounce must be wired on the global-search input.
    Empty value clears search instantly; non-empty schedules after
    `CHATS_SEARCH_DEBOUNCE_MS` ms.
    """
    assert "_chatsScheduleSearch" in chats_js
    assert "CHATS_SEARCH_DEBOUNCE_MS" in chats_js
    assert "addEventListener('input', _chatsScheduleSearch)" in chats_js
    # Empty-string keystroke must NOT debounce — it has to clear
    # immediately so the listing snaps back.
    m = re.search(
        r"function _chatsScheduleSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    assert "chatsClearSearch" in body
    assert "setTimeout" in body  # the debounce itself


def test_slash_key_focuses_search(chats_js: str) -> None:
    """`/` keypress on the Chats tab must focus + select the global
    search input. Mimics GitHub / Slack convention.
    """
    # Locate the slash handler.
    assert "ev.key !== '/'" in chats_js
    assert "input.focus()" in chats_js
    assert "input.select()" in chats_js


def test_jk_arrow_keyboard_navigation_through_cards(chats_js: str) -> None:
    """j/k/ArrowDown/ArrowUp move focus; Enter opens the focused card.

    Direction is INVERTED-vim per the user's preference recorded in
    CLAUDE.local.md: j moves UP (decrements focusIndex), k moves DOWN
    (increments). The arrow keys keep their literal direction.
    """
    assert "_chatsSetFocus" in chats_js
    assert "chatsState.focusIndex" in chats_js
    # Each binding must be present.
    assert "ev.key === 'j'" in chats_js
    assert "ev.key === 'k'" in chats_js
    assert "ev.key === 'ArrowDown'" in chats_js
    assert "ev.key === 'ArrowUp'" in chats_js
    assert "ev.key === 'Enter'" in chats_js
    # Enter must open via selectChat.
    assert "selectChat(card.dataset.sid)" in chats_js

    # Inverted-vim direction lock-in: k is paired with ArrowDown
    # (increments), j is paired with ArrowUp (decrements).
    down_branch = re.search(
        r"if \(ev\.key === '([jk])' \|\| ev\.key === 'ArrowDown'\)\s*\{(.*?)\}",
        chats_js,
        re.DOTALL,
    )
    up_branch = re.search(
        r"if \(ev\.key === '([jk])' \|\| ev\.key === 'ArrowUp'\)\s*\{(.*?)\}",
        chats_js,
        re.DOTALL,
    )
    assert down_branch is not None and down_branch.group(1) == 'k', (
        "k must be paired with ArrowDown (inverted-vim: k goes down)"
    )
    assert up_branch is not None and up_branch.group(1) == 'j', (
        "j must be paired with ArrowUp (inverted-vim: j goes up)"
    )
    # Direction confirms: down increments, up decrements.
    assert "focusIndex + 1" in down_branch.group(2)
    assert "focusIndex - 1" in up_branch.group(2)


def test_chat_card_focused_class_has_distinct_style(styles_text: str) -> None:
    """Focused (keyboard cursor) and active (open in viewer) must be
    visually distinct so the user can tell where they are vs what they
    have open. Both styles must be present in the CSS.
    """
    if not styles_text:
        pytest.skip("styles module didn't expose its text")
    assert ".chat-card.focused" in styles_text
    assert ".chat-card.active" in styles_text


def test_hash_persistence_writes_q_and_days(chats_js: str) -> None:
    """chatsGlobalSearch and chatsClearSearch must call _persistHash
    so the URL stays in sync with search state. days dropdown change
    also triggers persistence.
    """
    assert "_persistHash()" in chats_js
    # Specifically inside chatsGlobalSearch.
    m = re.search(
        r"async function chatsGlobalSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    assert "_persistHash" in m.group(1)


def test_match_highlighting_wraps_in_mark(chats_js: str, styles_text: str) -> None:
    """Snippet renderer must wrap matches in <mark>; CSS must style
    that <mark> with a quiet (not loud-orange) treatment.
    """
    assert "_chatsRenderSnippet" in chats_js
    assert "<mark>" in chats_js  # the regex replacement template
    if styles_text:
        # Style hook present — the snippet's mark uses accent-subtle
        # background, not accent (orange).
        assert ".chunk-snippet mark" in styles_text or "chat-card-chunk" in styles_text


def test_snippet_renderer_centers_on_first_match(chats_js: str) -> None:
    """When the matched token appears past the snippet half-width,
    the renderer should slide the window to center on the match
    instead of always starting at character 0.
    """
    m = re.search(
        r"function _chatsRenderSnippet\(rawText\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    assert "firstMatch" in body
    # Centering math: start = max(0, firstMatch - WIDTH/2).
    assert "WIDTH / 2" in body or "WIDTH/2" in body


# ---------------------------------------------------------------------------
# Two-view discipline: viewer mode hides the cross-session toolbar
# ---------------------------------------------------------------------------


def test_viewer_mode_class_toggles_on_select_and_close(chats_js: str) -> None:
    """selectChat must enter viewer mode via the shared helper (which
    adds `.chats-tab--viewer`); closeChat must remove it. That class
    is the CSS hook that hides the toolbar so the chat-detail view
    doesn't get a stale "find a different chat" bar sitting on top of
    it.
    """
    # selectChat side — funnels through the shared helper.
    m_sel = re.search(
        r"async function selectChat\(sessionId\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_sel is not None
    assert "_chatsEnterViewerMode" in m_sel.group(1)

    # closeChat side: removes the class directly (one place).
    m_close = re.search(
        r"function closeChat\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_close is not None
    assert "classList.remove('chats-tab--viewer')" in m_close.group(1)


def test_viewer_mode_css_hides_toolbar_and_pager(styles_text: str) -> None:
    """The `.chats-tab--viewer` class on #panel-chats must hide the
    cross-session toolbar (search/filters/sort/days/Advanced) AND the
    listing pager. Otherwise opening a chat leaves stale "find another
    chat" UI on top of the detail view.
    """
    if not styles_text:
        pytest.skip("styles module didn't expose its text")
    # Find the rule that targets `.chats-tab--viewer .chats-toolbar`.
    assert ".chats-tab--viewer .chats-toolbar" in styles_text
    assert ".chats-tab--viewer .chats-advanced-panel" in styles_text
    assert "#chats-pager" in styles_text  # the pager mount-point is styled


def test_back_to_list_button_replaces_floating_close_x(
    panel_html: str, styles_text: str,
) -> None:
    """The old floating X close button overlapped the role-filter pills
    (User / Assistant / All) on the viewer header's right edge. Its
    replacement is a clear "Back to all chats" button ABOVE the header.
    """
    # Old floating-X class is gone — both the element and its CSS rule.
    assert "chats-close-btn" not in panel_html
    if styles_text:
        assert ".chats-close-btn" not in styles_text

    # New back-bar element is present and wired to closeChat().
    assert "chats-viewer-backbar" in panel_html
    assert "chats-back-btn" in panel_html
    assert "Back to all chats" in panel_html
    # Backbar precedes the viewer header in source order.
    backbar_idx = panel_html.find("chats-viewer-backbar")
    header_idx = panel_html.find('id="chats-viewer-header"')
    assert 0 < backbar_idx < header_idx, (
        "back-bar must appear BEFORE the viewer header so the affordance "
        "lives above the header, not floating over its buttons"
    )


# ---------------------------------------------------------------------------
# In-chat search: hits row BELOW the input bar, not to the right of it
# ---------------------------------------------------------------------------


def test_in_chat_search_hits_render_below_search_bar(
    panel_html: str, styles_text: str,
) -> None:
    """The in-chat (within-conversation) match list must sit on its OWN
    row below the input + Find + Close buttons, not flex-wrap to the
    right of them. The structural fix is the bar/hits split inside
    `.chats-in-search` and the corresponding column layout on the
    parent.
    """
    # HTML scaffold: input lives inside .chats-in-search-bar, hits in
    # a sibling .chats-in-search-hits div (NOT inside the bar).
    assert "chats-in-search-bar" in panel_html
    bar_idx = panel_html.find('class="chats-in-search-bar"')
    hits_idx = panel_html.find('id="chats-in-search-hits"')
    assert 0 < bar_idx < hits_idx, (
        "in-chat search HITS div must appear AFTER the input bar in "
        "source order so it renders on its own row below"
    )

    # CSS: parent flex-direction is column so children stack vertically.
    if styles_text:
        m = re.search(r"\.chats-in-search\s*\{([^}]+)\}", styles_text)
        assert m is not None
        body = m.group(1)
        assert "flex-direction: column" in body, (
            "chats-in-search must lay out its children in a column so "
            "the hits row stacks below the input bar"
        )


# ---------------------------------------------------------------------------
# Sort by most-recent ACTIVITY (end_time), not chat-creation (start_time)
# ---------------------------------------------------------------------------


def test_recent_sort_uses_end_time(chats_js: str) -> None:
    """The 'Most Recent' sort key must be end_time (last message
    timestamp), not start_time. Users expect 'most recent' to mean
    'most recently active' — including chats that were started days
    ago but had a new turn this morning.
    """
    # Locate the renderChatList sort block.
    m = re.search(
        r"if \(sort === 'recent'\)\s*\{(.*?)\}",
        chats_js,
        re.DOTALL,
    )
    assert m is not None, "'recent' sort branch missing"
    body = m.group(1)
    assert "end_time" in body, "'recent' sort must use end_time"


def test_card_renders_both_start_and_end_timestamps(chats_js: str) -> None:
    """Every card must surface both timestamps — when the chat was
    created (start_time) AND when it was last active (end_time) — so
    the user can disambiguate at-a-glance which is which.

    Labels: "Last" (end_time) + "Started" (start_time). "Last" was
    chosen over "Active" after the user noted "Last" reads more
    clearly for a datetime — "Active" implied a status/state.
    """
    m = re.search(
        r"function renderChatCard\(c, searchHit\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    # Both timestamps are passed through formatTimestamp().
    assert "c.end_time" in body
    assert "c.start_time" in body
    # Each timestamp is labeled (Last / Started) so the user doesn't
    # have to guess which is which.
    assert ">Last</span>" in body, (
        "card timestamp label must read 'Last' (not 'Active' — that "
        "was reverted per user feedback)"
    )
    assert ">Started</span>" in body
    # CSS hook for the labels is present.
    assert "chat-card-time-label" in body


def test_toolbar_groups_widgets_for_responsive_collapse(
    panel_html: str, styles_text: str,
) -> None:
    """The toolbar must split its widgets into two semantic groups
    (search group + filter group) so the responsive breakpoints can
    stack ROWS instead of every-widget-on-its-own-line.

    Group 1 (.chats-toolbar-search-group): scopes what to find —
        global search input, search-method picker, project filter.
    Group 2 (.chats-toolbar-filter-group): scopes the rendering —
        sort, days, Advanced.
    """
    # Both group elements exist.
    assert "chats-toolbar-search-group" in panel_html
    assert "chats-toolbar-filter-group" in panel_html

    # Each widget lives inside the right group. Cheap source-order
    # test: each group div opens, then encloses the widget IDs we
    # expect, before the next group opens.
    search_start = panel_html.find("chats-toolbar-search-group")
    filter_start = panel_html.find("chats-toolbar-filter-group")
    assert 0 < search_start < filter_start, (
        "search group must precede filter group in source order"
    )

    # Widgets that BELONG to the search group:
    for el in ("chats-global-search", "chats-search-method",
               "chats-project-filter"):
        idx = panel_html.find('id="' + el + '"')
        assert search_start < idx < filter_start, (
            f"#{el} must live inside .chats-toolbar-search-group"
        )

    # Widgets that BELONG to the filter group:
    for el in ("chats-sort", "chats-days", "chats-advanced-toggle"):
        idx = panel_html.find('id="' + el + '"')
        assert idx > filter_start, (
            f"#{el} must live inside .chats-toolbar-filter-group"
        )

    # CSS: tiered breakpoints actually exist.
    if styles_text:
        # The 768px column-collapse (which caused the every-widget-
        # on-its-own-line bug) MUST be gone.
        assert re.search(
            r"@media \(max-width: 768px\) \{[^}]*\.chats-toolbar \{",
            styles_text,
        ) is None, (
            "the 768px column-collapse rule was the bug — must be "
            "replaced by tiered group-stacking breakpoints"
        )
        # Tier 1 (medium, ~900px): stack the two groups.
        assert re.search(
            r"@media \(max-width: 900px\)", styles_text,
        ) is not None
        # Tier 2 (narrow, ~500px): break the input out of its group.
        assert re.search(
            r"@media \(max-width: 500px\)", styles_text,
        ) is not None
        # The legacy spacer must be removed from the HTML.
        assert "chats-toolbar-spacer" not in panel_html


def test_sid_chip_click_copies_to_clipboard(
    chats_js: str, styles_text: str,
) -> None:
    """Clicking a session-id chip copies the full UUID to the
    clipboard with a brief "Copied!" flash on the chip.

    The chip is `<code class="chat-card-sid">` and the listener
    wiring must:
      * stop event propagation so the card itself doesn't open
      * call into a helper that uses navigator.clipboard.writeText
      * flash a CSS class on the chip for visual feedback
    """
    # Helper exists and uses the async clipboard API + legacy fallback.
    m = re.search(
        r"function _chatsCopySid\(el\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "_chatsCopySid helper missing"
    body = m.group(1)
    assert "navigator.clipboard" in body
    assert "writeText" in body
    assert "chat-card-sid--copied" in body, (
        "must flash a class on the chip so the user gets visual "
        "feedback that the copy succeeded"
    )

    # Legacy fallback for older browsers / non-secure origins.
    assert "_chatsLegacyCopy" in chats_js
    assert "execCommand" in chats_js

    # The renderer wires the listener on every .chat-card-sid in
    # the container.
    assert "querySelectorAll('.chat-card-sid')" in chats_js
    # ev.stopPropagation must guard the chip click so it doesn't
    # ALSO open the chat-detail viewer.
    m2 = re.search(
        r"querySelectorAll\('\.chat-card-sid'\).*?addEventListener.*?function\(ev\)\s*\{(.*?)\}\);",
        chats_js,
        re.DOTALL,
    )
    assert m2 is not None
    assert "stopPropagation" in m2.group(1)

    # CSS: the flash class is styled.
    if styles_text:
        assert ".chat-card-sid--copied" in styles_text


def test_sort_dropdown_locks_to_relevance_when_searching(
    panel_html: str, chats_js: str, styles_text: str,
) -> None:
    """When a search activates, the sort dropdown auto-selects
    "Most Relevant", disables interaction, and stashes the user's
    prior choice. When the search clears, the prior choice is
    restored.

    Previously: sort was silently overridden by doc_score and the
    dropdown gave the user no feedback for changing it. Confusing.
    """
    # The hidden "Most Relevant" option is present in the dropdown.
    m = re.search(
        r'<option value="relevance"[^>]*hidden[^>]*>Most Relevant</option>',
        panel_html,
    )
    assert m is not None, (
        "The 'Most Relevant' option must be declared with hidden so "
        "it doesn't appear in the menu when no search is active. JS "
        "unhides it during lock and re-hides on unlock."
    )

    # Lock helper exists and does the four things it must:
    m_lock = re.search(
        r"function _chatsLockSortToRelevance\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_lock is not None
    lb = m_lock.group(1)
    assert "_priorSort" in lb, "must stash the prior sort value"
    assert "value = 'relevance'" in lb, "must set the dropdown value"
    assert "disabled = true" in lb, "must disable the dropdown"
    assert "chats-sort--locked" in lb, "must add the locked class"

    # Unlock helper exists and restores:
    m_unlock = re.search(
        r"function _chatsUnlockSort\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_unlock is not None
    ub = m_unlock.group(1)
    assert "disabled = false" in ub
    assert "_priorSort" in ub
    assert "chats-sort--locked" in ub

    # chatsGlobalSearch calls Lock; chatsClearSearch calls Unlock.
    m_search = re.search(
        r"async function chatsGlobalSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_search is not None
    assert "_chatsLockSortToRelevance" in m_search.group(1)

    m_clear = re.search(
        r"function chatsClearSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_clear is not None
    assert "_chatsUnlockSort" in m_clear.group(1)

    # CSS gives a visual signal for the locked state.
    if styles_text:
        assert ".chats-sort--locked" in styles_text


def test_search_method_change_refires_active_search(
    chats_js: str,
) -> None:
    """Changing the search method (Hybrid / Keyword / Semantic) while
    a search is active should re-fire the query under the new method.
    Was silently no-op — the user had to retype to actually see the
    method swap take effect.
    """
    m = re.search(
        r"function chatsSearchMethodChanged\(method\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    # Must re-fire chatsGlobalSearch when searchActive + query are set.
    assert "chatsState.searchActive" in body
    assert "chatsGlobalSearch" in body, (
        "method change must trigger a search re-fire so the user sees "
        "an immediate update"
    )


def test_card_shows_full_session_id_in_monospace(
    chats_js: str, styles_text: str,
) -> None:
    """Each card renders the full session UUID at the top-right in
    a monospace ("mechanical") font. This is the at-a-glance
    differentiator for forked Claude Code sessions, which share the
    first-message + start_time but differ in session_id.

    Without this, forks look like card duplicates (same project,
    same title, same `Started`) and the user can't tell them apart
    short of opening each one.
    """
    # Renderer emits a header row holding both project + sid chip.
    m = re.search(
        r"function renderChatCard\(c, searchHit\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    assert "chat-card-header-row" in body, (
        "header-row container missing — project tag + session-id "
        "anchor live on the same flex row"
    )
    assert "chat-card-sid" in body
    # The chip wraps the FULL session_id (not the truncated short_id).
    assert "c.session_id" in body and "<code" in body
    # Not the short_id — that's a different field on the response.
    sid_chunk = re.search(
        r'class="chat-card-sid"[^>]*>[^<]*\+\s*escapeHtml\((c\.\w+)\)',
        body,
    )
    assert sid_chunk is not None and sid_chunk.group(1) == "c.session_id", (
        "session-id chip must render the FULL session_id (not short_id)"
    )

    # CSS: monospace font + the chip styling exist.
    if styles_text:
        m_css = re.search(
            r"\.chat-card-sid\s*\{([^}]+)\}", styles_text,
        )
        assert m_css is not None
        css_body = m_css.group(1)
        assert "font-family" in css_body
        assert "monospace" in css_body, (
            "session-id chip MUST use a monospace font so the UUID "
            "reads as a literal identifier"
        )

        # Header row uses flex with space-between so project anchors
        # left and the sid chip anchors right.
        m_row = re.search(
            r"\.chat-card-header-row\s*\{([^}]+)\}", styles_text,
        )
        assert m_row is not None
        row_body = m_row.group(1)
        assert "display: flex" in row_body
        assert "space-between" in row_body


def test_date_buckets_key_on_end_time(chats_js: str) -> None:
    """The day-group headers must bucket cards by end_time (last
    activity) so the headers actually correspond to the sort key.
    Bucketing by start_time would place "Today" cards under "3 days
    ago" if they were started 3 days back but used this morning.
    """
    m = re.search(
        r"if \(groupByDate\)\s*\{(.*?)\}", chats_js, re.DOTALL,
    )
    assert m is not None
    body = m.group(1)
    assert "_chatsBucketLabel(c.end_time" in body


# ---------------------------------------------------------------------------
# Explicit Search button + extended debounce
# ---------------------------------------------------------------------------


def test_search_send_button_is_inline_inside_input_wrap(
    panel_html: str, styles_text: str,
) -> None:
    """Subtle inline send affordance pinned to the right edge of the
    search input — not a separate full-size toolbar widget.

    Structure: a `.chats-search-input-wrap` div wraps the input AND a
    `.chats-search-send` button. CSS absolute-positions the button
    inside the input's frame so it reads as part of the input rather
    than a sibling toolbar widget.
    """
    # The wrap exists, holds both the input and the send button.
    assert "chats-search-input-wrap" in panel_html
    assert 'id="chats-search-send"' in panel_html
    # The OLD external Search button is gone — we replaced it with
    # the inline affordance.
    assert 'id="chats-search-btn"' not in panel_html
    assert 'class="chats-select chats-search-btn"' not in panel_html

    # Source-order: send button appears inside the wrap, AFTER the
    # input. Otherwise it would render to the left of the input.
    wrap_idx = panel_html.find("chats-search-input-wrap")
    input_idx = panel_html.find('id="chats-global-search"')
    send_idx = panel_html.find('id="chats-search-send"')
    assert wrap_idx > 0 < input_idx < send_idx, (
        "send button must sit AFTER the input inside the wrap"
    )

    # Hover tooltip is set via data-tooltip="Send · Enter". CSS-only
    # tooltip pattern; no JS lib required.
    assert 'data-tooltip="Send' in panel_html

    # CSS: the wrap is position-relative so absolute children anchor
    # inside it; the send button is position-absolute on the right.
    if styles_text:
        m = re.search(
            r"\.chats-search-input-wrap\s*\{([^}]+)\}", styles_text,
        )
        assert m is not None
        assert "position: relative" in m.group(1)
        m2 = re.search(
            r"\.chats-search-send\s*\{([^}]+)\}", styles_text,
        )
        assert m2 is not None
        body2 = m2.group(1)
        assert "position: absolute" in body2
        assert "right:" in body2


def test_search_send_button_calls_global_search(panel_html: str) -> None:
    """The inline send button must fire chatsGlobalSearch() — the same
    function Enter and the debounce both route through. Otherwise it
    becomes a fourth code path that can drift out of sync.
    """
    # Locate the send button tag and check its delegated action (wired via
    # data-on-click, not an inline onclick, since the frontend hardening).
    m = re.search(
        r'<button[^>]*id="chats-search-send"[^>]*>', panel_html,
    )
    assert m is not None
    tag = m.group(0)
    assert 'data-on-click="chatsGlobalSearch"' in tag


def test_search_debounce_relaxed(chats_js: str) -> None:
    """600ms felt too eager (fired mid-typing). 1500ms is the new
    floor — long enough that a natural typing pause doesn't trigger
    a stale-prefix search.
    """
    m = re.search(
        r"const CHATS_SEARCH_DEBOUNCE_MS = (\d+);", chats_js,
    )
    assert m is not None, "CHATS_SEARCH_DEBOUNCE_MS constant missing"
    delay = int(m.group(1))
    assert delay >= 1000, (
        f"as-you-type debounce ({delay}ms) must be >= 1000ms — anything "
        "shorter fires on natural typing pauses and confuses the user"
    )


# ---------------------------------------------------------------------------
# "Outside days window" hint must NOT show when user is on All time
# ---------------------------------------------------------------------------


def test_outside_window_hint_suppressed_on_all_time(chats_js: str) -> None:
    """The "show All time" hint is meaningless when the user is
    already on All time (days=0). Show it ONLY when days > 0.

    Otherwise the user sees "8 more outside the current days window"
    while already at the widest window — confusing and looks like a
    bug.
    """
    # Locate the renderChatList body so we can scope the assertion.
    m = re.search(
        r"function renderChatList\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    # The hint is gated on an "onAllTime" check that's only true when
    # the days dropdown is 0.
    assert "onAllTime" in body
    assert "daysVal === 0" in body or "daysVal == 0" in body
    # Concretely: the conditional must reference onAllTime.
    cond = re.search(
        r"if \(hiddenByWindow > 0 && !onAllTime\)", body,
    )
    assert cond is not None, (
        "the 'outside days window' hint must be guarded with !onAllTime"
    )


# ---------------------------------------------------------------------------
# Every viewer-entry path must apply .chats-tab--viewer (toolbar-hide)
# ---------------------------------------------------------------------------


def test_shared_viewer_mode_helper_exists(chats_js: str) -> None:
    """Every code path that opens the chat-detail view MUST route
    through one shared helper that applies the .chats-tab--viewer
    class. Bypassing the helper (as chatsJumpToHit did) leaves the
    cross-session toolbar + pager visible on top of the viewer.
    """
    # Helper exists and adds the class.
    m = re.search(
        r"function _chatsEnterViewerMode\(opts\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "_chatsEnterViewerMode helper not found"
    body = m.group(1)
    assert "classList.add('chats-tab--viewer')" in body, (
        "the shared helper is the one place that adds the viewer-mode "
        "class"
    )

    # All three viewer-entry paths must call the helper.
    for fn in ("selectChat", "chatsJumpToHit", "chatsJumpToCommitSearch"):
        m = re.search(
            r"async function " + re.escape(fn) + r"\([^)]*\)\s*\{(.*?)^\}",
            chats_js,
            re.DOTALL | re.MULTILINE,
        )
        assert m is not None, f"{fn} not found"
        assert "_chatsEnterViewerMode" in m.group(1), (
            f"{fn} must call _chatsEnterViewerMode (otherwise the "
            ".chats-tab--viewer class isn't applied and the toolbar + "
            "pager stay visible on top of the chat-detail view)"
        )


# ---------------------------------------------------------------------------
# In-flight search cancellation: keystrokes abort the running fetch
# ---------------------------------------------------------------------------


def test_inflight_search_can_be_aborted(chats_js: str) -> None:
    """An AbortController must be used so that mid-typing keystrokes
    actually cancel an in-flight IR request — not just drop its
    result on arrival. Otherwise the user racks up backend work for
    queries they're already abandoning.
    """
    # The cancel helper exists and tries to abort the stored controller.
    m = re.search(
        r"function _chatsAbortInflight\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None, "_chatsAbortInflight helper missing"
    body = m.group(1)
    assert "_chatsInflightAbort" in body
    assert ".abort()" in body

    # chatsGlobalSearch creates a NEW AbortController per call and
    # stores it on the module-level slot so subsequent calls can abort it.
    m_search = re.search(
        r"async function chatsGlobalSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m_search is not None
    sb = m_search.group(1)
    assert "new AbortController()" in sb, (
        "chatsGlobalSearch must construct an AbortController for its fetch"
    )
    assert "_chatsInflightAbort = controller" in sb
    assert "signal: controller.signal" in sb, (
        "the fetch must pass the AbortController's signal so the request "
        "is actually cancellable"
    )

    # Stale-completion guard — even if abort races and our await still
    # resolves, the result must NOT be allowed to overwrite state from
    # a newer query.
    assert "_chatsInflightAbort !== controller" in sb, (
        "stale-completion guard missing: a late-arriving response from "
        "a superseded query could overwrite the listing"
    )

    # AbortError branch returns early — don't render an error.
    assert "AbortError" in sb


def test_keystroke_aborts_inflight_search(chats_js: str) -> None:
    """The debounce scheduler must abort any in-flight request on
    every new keystroke — that's the resource-protection invariant
    the user asked for. Without this, a request whose 1500ms
    debounce window passed is still on the wire when the user
    resumes typing.
    """
    m = re.search(
        r"function _chatsScheduleSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    body = m.group(1)
    assert "_chatsAbortInflight()" in body, (
        "scheduler must call _chatsAbortInflight on every keystroke"
    )


def test_clear_search_also_aborts(chats_js: str) -> None:
    """Clearing the search (Esc, clear-button, empty-input) must
    abort any in-flight fetch — otherwise a late resolution could
    re-flip searchActive against a query the user explicitly cleared.
    """
    m = re.search(
        r"function chatsClearSearch\(\)\s*\{(.*?)^\}",
        chats_js,
        re.DOTALL | re.MULTILINE,
    )
    assert m is not None
    assert "_chatsAbortInflight()" in m.group(1)


def test_viewer_entry_paths_do_not_manually_toggle_panel_class(
    chats_js: str,
) -> None:
    """Belt-and-suspenders: only the shared helper should add the
    .chats-tab--viewer class. If any other function adds it directly,
    that's a sign someone bypassed the helper and is duplicating its
    job (the original bug).
    """
    occurrences = re.findall(
        r"classList\.add\('chats-tab--viewer'\)", chats_js,
    )
    # Exactly one — inside _chatsEnterViewerMode. closeChat does the
    # opposite (.remove), which is counted separately.
    assert len(occurrences) == 1, (
        f"classList.add('chats-tab--viewer') appears {len(occurrences)}× "
        "in chats.py; should be exactly once (inside "
        "_chatsEnterViewerMode). Multiple call sites means the shared "
        "helper is being bypassed."
    )
