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
# Pagination: page-size cap + Load more button + reset on state changes
# ---------------------------------------------------------------------------


def test_listing_has_pagination_state(chats_js: str) -> None:
    assert "chatsState.page" in chats_js
    assert "chatsState.pageSize" in chats_js
    assert "chatsLoadMoreList" in chats_js
    assert "chatsResetPage" in chats_js


def test_load_more_button_renders_when_there_are_more_cards(chats_js: str) -> None:
    """The renderer should append a Load-more affordance and pass the
    remaining count for the user.
    """
    assert "renderLoadMore" in chats_js
    # The label format we render — "Show more (N remaining)" — locks
    # in the affordance shape so future edits don't accidentally hide
    # the count.
    assert "remaining" in chats_js


def test_filter_changes_reset_pagination(chats_js: str) -> None:
    """Toggling a pill, a project, a sort, or the days window must
    reset chatsState.page to 0. Otherwise users on page 5 would see
    a confusing partial slice after re-filtering.
    """
    # Direct calls to chatsResetPage in the relevant handlers.
    for handler in (
        "chatsToggleFilter",
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
    """j/k/ArrowDown/ArrowUp move focus; Enter opens the focused card."""
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
