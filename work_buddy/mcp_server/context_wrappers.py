"""Gateway-callable wrappers for context collectors.

Each wrapper adapts a collector's ``collect(cfg) -> str`` interface into a
function with user-facing keyword parameters.  Config is loaded internally
so these can be called directly via ``wb_run``.

Design tenets (see dev/design-tenets in knowledge store):
- **Progressive disclosure** — each collector is its own capability
- **Just-in-time retrieval** — returns data directly, not file paths
- **Programmatic offloading** — deterministic code, not workflow steps
"""

from __future__ import annotations

from typing import Any


def _cfg_with_overrides(**overrides: Any) -> dict[str, Any]:
    """Load config and apply nested key overrides.

    Accepts flattened dotpath overrides like ``git__detail_days=3`` (double
    underscore = nesting) plus top-level keys like ``since`` / ``until``.
    """
    from work_buddy.config import load_config

    cfg = load_config()
    for key, val in overrides.items():
        if val is None:
            continue
        parts = key.split("__")
        d = cfg
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = val
    return cfg


# ---------------------------------------------------------------------------
# Individual collector wrappers
# ---------------------------------------------------------------------------

def get_git_context(*, days: int = 7, dirty_only: bool = False, annotate: bool = False) -> str:
    """Recent git activity across all repos: commits, diffs, dirty trees.

    Args:
        days: Lookback window for detailed commit history.
        dirty_only: If true, only include repos with uncommitted changes.
        annotate: If true, tag commits made by agent sessions with their
            session ID.  Slower — scans JSONL session files.
    """
    from work_buddy.collectors import git_collector

    cfg = _cfg_with_overrides(
        git__detail_days=days,
        git__dirty_only=dirty_only,
    )
    # Keep active_days at least as wide as detail_days
    cfg["git"]["active_days"] = max(days, cfg["git"].get("active_days", 30))

    session_map = None
    if annotate:
        from work_buddy.sessions.inspector import build_session_map
        session_map = build_session_map(days=days)

    return git_collector.collect(cfg, session_map=session_map)


def get_obsidian_context(*, journal_days: int = 7, modified_days: int = 3) -> str:
    """Obsidian vault summary: journal entries, recently modified notes.

    Args:
        journal_days: Days of journal entries to include.
        modified_days: Days of recently modified files to include.
    """
    from work_buddy.collectors import obsidian_collector

    cfg = _cfg_with_overrides(
        obsidian__journal_days=journal_days,
        obsidian__recent_modified_days=modified_days,
    )
    obsidian_md, _tasks_md = obsidian_collector.collect(cfg)
    return obsidian_md


def get_tasks_context(*, journal_days: int = 7, event_hours: int | None = None) -> str:
    """Obsidian task summary: outstanding tasks + recent state changes.

    Args:
        journal_days: Days of journal entries to scan for tasks.
        event_hours: Override the default lookback for task state events
            (default from config: 48h). Pass 0 to suppress events.
    """
    from work_buddy.collectors import obsidian_collector

    overrides: dict[str, Any] = {"obsidian__journal_days": journal_days}
    if event_hours is not None:
        overrides["tasks__event_lookback_hours"] = event_hours
    cfg = _cfg_with_overrides(**overrides)
    _obsidian_md, tasks_md = obsidian_collector.collect(cfg)
    return tasks_md


def get_wellness_context(*, days: int = 14) -> str:
    """Wellness tracker summary from recent journal entries.

    Args:
        days: Days of wellness data to include.
    """
    from work_buddy.collectors import obsidian_collector

    cfg = _cfg_with_overrides(obsidian__wellness_days=days)
    return obsidian_collector.collect_wellness(cfg)


def get_chat_context(*, days: int = 7, last: int | None = None) -> str:
    """Recent Claude Code conversations and CLI history.

    Returns a markdown summary of recent sessions with tool usage,
    duration, and outcome snippets.

    Args:
        days: Lookback window for session discovery.
        last: Cap the number of sessions returned per source.
    """
    from work_buddy.collectors import chat_collector

    overrides: dict[str, Any] = {
        "chats__claude_history_days": days,
        "chats__specstory_days": days,
    }
    if last is not None:
        overrides["chats__last"] = last
    cfg = _cfg_with_overrides(**overrides)
    return chat_collector.collect(cfg)


def get_chrome_context() -> str:
    """Currently open Chrome tabs (requires Chrome extension running)."""
    from work_buddy.collectors import chrome_collector

    cfg = _cfg_with_overrides()
    return chrome_collector.collect(cfg)


def chrome_activity(
    *,
    query: str = "hot_tabs",
    since: str = "2h",
    until: str | None = None,
    limit: int = 20,
    timestamp: str | None = None,
    filter: str | None = None,
) -> str:
    """Query Chrome tab browsing history from the rolling ledger.

    The ledger captures tab snapshots every 5 minutes. This capability
    provides computed views over that raw data. Output is compact (titles
    + domains, no full URLs) to save context. Use ``details`` query to
    get full URLs when needed.

    Args:
        query: Query type — one of:
            ``hot_tabs`` (default): tabs ranked by engagement.
            ``changes``: tabs opened/closed/navigated in the window.
            ``sessions``: browsing activity grouped by domain.
            ``tabs_at``: what tabs were open at a specific time (requires ``timestamp``).
            ``context``: tab proximity and window layout at a point in time — shows
                adjacent tab clusters that may indicate related work.
            ``details``: full URLs for tabs matching ``filter`` (domain or title substring).
            ``status``: ledger health and stats.
        since: Start of window. Relative shorthand (``2h``, ``1d``, ``30m``) or ISO datetime.
        until: End of window. Default: now.
        limit: Max results for hot_tabs (default 20).
        timestamp: Required for ``tabs_at`` query — ISO datetime or relative shorthand.
        filter: For ``details`` query — domain or title substring to match.
    """
    import json
    import re as _re
    from datetime import timedelta

    from work_buddy.collectors import chrome_ledger
    from work_buddy.journal import user_now

    now = user_now().replace(tzinfo=None)

    def _parse_relative(val: str) -> str:
        """Convert relative shorthand to ISO datetime string."""
        rel = _re.fullmatch(r"(\d+)\s*(m|min|h|hour|hours|d|day|days)", val.strip())
        if rel:
            amount = int(rel.group(1))
            unit = rel.group(2)[0]
            deltas = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}
            return (now - deltas[unit]).isoformat()
        return val  # assume ISO

    since_iso = _parse_relative(since)
    until_iso = _parse_relative(until) if until else now.isoformat()

    if query == "hot_tabs":
        result = chrome_ledger.get_hot_tabs(since_iso, until_iso, limit=limit)
        return _format_hot_tabs(result)
    elif query == "changes":
        result = chrome_ledger.get_tab_changes(since_iso, until_iso)
        return _format_tab_changes(result)
    elif query == "sessions":
        result = chrome_ledger.get_tab_sessions(since_iso, until_iso)
        return _format_tab_sessions(result)
    elif query == "tabs_at":
        ts = _parse_relative(timestamp) if timestamp else now.isoformat()
        result = chrome_ledger.get_tabs_at(ts)
        return _format_tabs_at(result)
    elif query == "context":
        ts = _parse_relative(timestamp) if timestamp else None
        result = chrome_ledger.get_tab_context(ts)
        return _format_tab_context(result)
    elif query == "details":
        if not filter:
            return "The `details` query requires a `filter` parameter (domain or title substring)."
        ts = _parse_relative(timestamp) if timestamp else now.isoformat()
        result = chrome_ledger.get_tabs_at(ts)
        return _format_tab_details(result, filter)
    elif query == "status":
        result = chrome_ledger.ledger_status()
        return json.dumps(result, indent=2)
    else:
        return f"Unknown query type: {query}. Use: hot_tabs, changes, sessions, tabs_at, details, status."


def _short_domain(url: str) -> str:
    """Extract a compact domain label from a URL."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc
        # Strip www.
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _compact_tab(title: str, url: str, max_title: int = 60) -> str:
    """Format a tab as 'Title [domain]' — no full URL."""
    domain = _short_domain(url)
    if len(title) > max_title:
        title = title[:max_title - 3] + "..."
    if domain:
        return f"{title} [{domain}]"
    return title


def _format_hot_tabs(data: dict) -> str:
    """Format hot_tabs result as markdown. Compact: titles + domains, no URLs."""
    tabs = data.get("tabs", [])
    if not tabs:
        return "No Chrome tab data in this window. Is the extension running with periodic snapshots enabled?"

    lines = [f"## Hot Tabs ({data['total_snapshots']} snapshots, {data['total_unique_urls']} unique URLs)", ""]
    for t in tabs:
        open_min = t.get("estimated_open_minutes", 0)
        active = t.get("active_count", 0)
        engaged = t.get("engaged_count", 0)
        visits = t.get("visit_count", 0)
        label = _compact_tab(t.get("title", "") or t.get("domain", ""), t.get("url", ""))
        details = []
        if visits:
            details.append(f"{visits} visits")
        if engaged:
            details.append(f"engaged {engaged}x")
        if active:
            details.append(f"focused {active}x")
        if open_min:
            details.append(f"open {open_min} min")
        lines.append(f"- **{label}** ({', '.join(details)})")
    lines.append("")
    lines.append("*Use `query='details', timestamp='...'` to get full URLs for specific tabs.*")
    return "\n".join(lines)


def _format_tab_changes(data: dict) -> str:
    """Format tab_changes result as markdown. Compact: no full URLs."""
    lines = [f"## Tab Changes ({data['snapshots_compared']} snapshots compared)", ""]

    engaged = data.get("engaged", [])
    opened = data.get("opened", [])
    closed = data.get("closed", [])
    navigated = data.get("navigated", [])

    if engaged:
        lines.append(f"### Engaged ({len(engaged)} tabs interacted with)")
        for t in engaged[:20]:
            count = t.get("interaction_count", 0)
            active_during = t.get("active_during_interaction", 0)
            intensity = t.get("intensity", 0)
            est_sec = t.get("estimated_active_seconds", 0)
            label = _compact_tab(t.get("title", ""), t.get("url", ""))
            parts = [f"{count}x"]
            if est_sec >= 60:
                parts.append(f"~{est_sec // 60}m active")
            elif est_sec > 0:
                parts.append(f"~{est_sec}s active")
            if active_during:
                parts.append(f"focused {active_during}x")
            if intensity >= 0.7:
                parts.append("sustained")
            elif intensity >= 0.3:
                parts.append("moderate")
            lines.append(f"- **{label}** ({', '.join(parts)})")
        lines.append("")

    if opened:
        lines.append(f"### Opened ({len(opened)})")
        for t in opened[:20]:
            lines.append(f"- {_compact_tab(t.get('title', ''), t.get('url', ''))}")
        lines.append("")

    if closed:
        lines.append(f"### Closed ({len(closed)})")
        for t in closed[:20]:
            lines.append(f"- {_compact_tab(t.get('title', ''), t.get('url', ''))}")
        lines.append("")

    if navigated:
        lines.append(f"### Navigated ({len(navigated)})")
        for t in navigated[:20]:
            lines.append(f"- {_compact_tab(t.get('to_title', ''), t.get('to_url', ''))}")
        lines.append("")

    moved = data.get("moved", [])
    if moved:
        lines.append(f"### Moved between windows ({len(moved)})")
        for t in moved[:20]:
            label = _compact_tab(t.get("title", ""), t.get("url", ""))
            lines.append(f"- {label}")
        lines.append("")

    visited = data.get("visited", [])
    if visited:
        lines.append(f"### Visited ({len(visited)} pages from browsing history)")
        for t in visited[:30]:
            count = t.get("visit_count", 1)
            count_str = f" ({count}x)" if count > 1 else ""
            lines.append(f"- {_compact_tab(t.get('title', ''), t.get('url', ''))}{count_str}")
        lines.append("")

    if not opened and not closed and not navigated and not engaged and not visited:
        lines.append("No tab activity detected in this window.")

    return "\n".join(lines)


def _format_tab_sessions(data: dict) -> str:
    """Format tab_sessions result as markdown."""
    sessions = data.get("sessions", [])
    if not sessions:
        return "No browsing sessions found in this window."

    lines = [f"## Browsing Sessions ({data['total_domains']} domains)", ""]
    for s in sessions[:20]:
        domain = s.get("domain", "")
        pages = s.get("page_count", 0)
        minutes = s.get("estimated_minutes", 0)
        engaged = s.get("engaged_count", 0)
        titles = s.get("sample_titles", [])
        title_str = f" — e.g. '{titles[0][:50]}'" if titles else ""
        engaged_str = f", engaged {engaged}x" if engaged else ""
        lines.append(f"- **{domain}** ({minutes} min, {pages} pages{engaged_str}){title_str}")
    return "\n".join(lines)


def _format_tabs_at(data: dict) -> str:
    """Format tabs_at result as markdown. Compact: no full URLs."""
    tabs = data.get("tabs", [])
    if not tabs:
        return "No snapshot found near that time."

    delta = data.get("time_delta_seconds", 0)
    lines = [
        f"## Tabs at {data['snapshot_time']} (nearest snapshot, {delta}s delta)",
        f"**{data['tab_count']} tabs**",
        "",
    ]
    for t in tabs:
        flags = []
        if t.get("active"):
            flags.append("**active**")
        if t.get("pinned"):
            flags.append("pinned")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {_compact_tab(t.get('title', 'Untitled'), t.get('url', ''))}{flag_str}")
    return "\n".join(lines)


def _format_tab_context(data: dict) -> str:
    """Format tab context/proximity analysis as markdown."""
    windows = data.get("windows", [])
    clusters = data.get("clusters", [])

    if not windows:
        return "No snapshot available for context analysis."

    lines = [f"## Tab Layout ({data.get('snapshot_time', 'latest')})", ""]

    # Show clusters first — the high-value signal
    if clusters:
        lines.append(f"### Same-domain clusters ({len(clusters)})")
        for c in clusters:
            titles = ", ".join(f"'{t}'" for t in c["titles"][:3])
            more = f" +{c['tab_count'] - 3} more" if c["tab_count"] > 3 else ""
            lines.append(f"- **{c['domain']}** ({c['tab_count']} adjacent tabs): {titles}{more}")
        lines.append("")

    # Show window layouts with position order
    for w in windows:
        lines.append(f"### Window ({w['tab_count']} tabs)")
        for t in w["tabs"]:
            domain = t.get("domain", "")
            title = t.get("title", "")[:50]
            flags = []
            if t.get("active"):
                flags.append("active")
            if t.get("group_id"):
                flags.append(f"group:{t['group_id']}")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {t['index']:2d}. {title} [{domain}]{flag_str}")
        lines.append("")

    return "\n".join(lines)


def _format_tab_details(data: dict, filter_str: str) -> str:
    """Return full URLs for tabs matching a domain or title filter."""
    tabs = data.get("tabs", [])
    if not tabs:
        return "No snapshot found."

    needle = filter_str.lower()
    matches = []
    for t in tabs:
        title = t.get("title", "")
        url = t.get("url", "")
        domain = _short_domain(url)
        if needle in title.lower() or needle in domain.lower() or needle in url.lower():
            matches.append(t)

    if not matches:
        return f"No tabs matching '{filter_str}' in the nearest snapshot."

    lines = [f"## Tab Details: '{filter_str}' ({len(matches)} matches)", ""]
    for t in matches:
        flags = []
        if t.get("active"):
            flags.append("active")
        if t.get("pinned"):
            flags.append("pinned")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {t.get('title', 'Untitled')}{flag_str}")
        lines.append(f"  `{t.get('url', '')}`")
    return "\n".join(lines)


def llm_costs(*, breakdown: bool = False) -> str:
    """Check LLM token usage and costs for this session.

    Args:
        breakdown: If true, show per-task and per-model breakdown with
            top callers. Otherwise show session totals only.
    """
    import json

    from work_buddy.llm.cost import session_breakdown, session_total

    if breakdown:
        data = session_breakdown()
        lines = ["## LLM Cost Breakdown", ""]

        total = data.get("total", {})
        lines.append(f"**Total:** {total.get('api_calls', 0)} API calls, "
                     f"{total.get('cache_hits', 0)} cache hits, "
                     f"${total.get('estimated_cost_usd', 0):.4f}")
        lines.append("")

        by_task = data.get("by_task", {})
        if by_task:
            lines.append("### By Task")
            for task, stats in sorted(by_task.items(), key=lambda x: x[1]["cost_usd"], reverse=True):
                lines.append(f"- **{task}**: {stats['api_calls']} API / {stats['cache_hits']} cached, "
                             f"{stats['input_tokens']} in + {stats['output_tokens']} out, "
                             f"${stats['cost_usd']:.4f}")
            lines.append("")

        by_model = data.get("by_model", {})
        if by_model:
            lines.append("### By Model")
            for model, stats in sorted(by_model.items(), key=lambda x: x[1]["cost_usd"], reverse=True):
                lines.append(f"- **{model}**: {stats['calls']} calls, ${stats['cost_usd']:.4f}")
            lines.append("")

        top_callers = data.get("top_callers", [])
        if top_callers:
            lines.append("### Top Callers")
            for c in top_callers[:5]:
                lines.append(f"- `{c['caller']}`: ${c['cost_usd']:.4f}")

        return "\n".join(lines)
    else:
        data = session_total()
        return (f"LLM costs this session: {data['api_calls']} API calls, "
                f"{data['cache_hits']} cache hits, "
                f"{data['total_input_tokens']} in + {data['total_output_tokens']} out tokens, "
                f"${data['estimated_cost_usd']:.4f}")


def chrome_infer(
    *,
    since: str = "1h",
    theories: str | None = None,
    tab_limit: int = 5,
) -> str:
    """Infer what the user is working on from engaged Chrome tabs.

    Reads page content from high-engagement tabs, sends to Haiku for
    analysis, and returns structured inferences. Results are cached per
    tab to avoid redundant API calls.

    Args:
        since: Lookback window. Relative ('1h', '30m') or ISO datetime.
        theories: Comma-separated theories to evaluate (e.g., "researching pricing, working on code").
        tab_limit: Max tabs to analyze (default 5).
    """
    import json

    from work_buddy.collectors.chrome_infer import infer_browsing_activity

    theory_list = None
    if theories:
        theory_list = [t.strip() for t in theories.split(",") if t.strip()]

    result = infer_browsing_activity(
        since=since,
        theories=theory_list,
        tab_limit=tab_limit,
    )

    if result.get("error"):
        return f"Chrome inference failed: {result['error']}"

    lines = [f"## Browsing Inference ({result.get('tabs_read', 0)} read, {result.get('tabs_cached', 0)} cached, {result.get('tabs_failed', 0)} failed)", ""]

    # Tab summaries
    summaries = result.get("summaries", [])
    if summaries:
        lines.append(f"### Tab Summaries ({len(summaries)})")
        for s in summaries:
            label = s.get("source_label", "")
            summary = s.get("content_summary", "")
            posture = s.get("user_posture", "")
            intent = s.get("user_intent_speculation", "")
            entities = s.get("entities", [])
            claims = s.get("key_claims", [])

            lines.append(f"\n**{label}** [{posture}]")
            lines.append(f"{summary}")
            if intent:
                lines.append(f"*Intent speculation: {intent}*")
            if entities:
                ent_strs = [f"{e['name']} ({e['type']}: {e['context']})" for e in entities[:4]]
                lines.append(f"Entities: {', '.join(ent_strs)}")
            if claims:
                for claim in claims[:3]:
                    lines.append(f"- {claim}")
        lines.append("")

    # Classification (if theories were provided)
    classification = result.get("classification")
    if classification:
        narrative = classification.get("overall_narrative", "")
        domains = classification.get("activity_domains", [])
        if narrative:
            lines.append(f"**Activity:** {narrative}")
            if domains:
                lines.append(f"**Domains:** {', '.join(domains)}")
            lines.append("")

        # Aggregate intent matches across items
        from collections import defaultdict
        intent_evidence: dict[str, list[dict]] = defaultdict(list)
        for item in classification.get("items", []):
            for match in item.get("intent_matches", []):
                intent_evidence[match["intent"]].append({
                    "label": item.get("label", ""),
                    "relevant": match.get("relevant", False),
                    "evidence": match.get("evidence", ""),
                    "strength": match.get("strength", ""),
                })

        if intent_evidence:
            lines.append("### Intent Classification")
            for intent, matches in intent_evidence.items():
                relevant = [m for m in matches if m["relevant"]]
                if relevant:
                    strength = relevant[0].get("strength", "")
                    strength_str = f" ({strength})" if strength else ""
                    evidence_items = [m["evidence"] for m in relevant if m["evidence"]]
                    lines.append(f"- **{intent}**: supported by {len(relevant)} items{strength_str}")
                    for ev in evidence_items[:3]:
                        lines.append(f"  - {ev}")
                else:
                    lines.append(f"- **{intent}**: not supported")
            lines.append("")

        tokens = classification.get("tokens", {})
        if tokens:
            lines.append(f"*Classification: {tokens.get('input', 0)} in / {tokens.get('output', 0)} out tokens*")

    return "\n".join(lines)


def triage_item_detail_wrapper(
    *,
    item_id: str,
    include_raw: bool = False,
    max_raw_chars: int = 5000,
) -> str:
    """Retrieve detail for a specific triage item (summary and/or raw content).

    Use during the review phase to inspect items with content gaps.
    Prefer summaries over raw content unless the detail matters.

    Args:
        item_id: The TriageItem ID (e.g., "tab_786de35645").
        include_raw: If True, also return raw page content.
        max_raw_chars: Max characters of raw content.
    """
    from work_buddy.triage.detail import triage_item_detail_capability
    return triage_item_detail_capability(
        item_id=item_id,
        include_raw=include_raw,
        max_raw_chars=max_raw_chars,
    )


def chrome_content(
    *,
    tab_filter: str | None = None,
    tab_limit: int = 5,
    max_chars: int = 5000,
) -> str:
    """Extract page text from currently-open Chrome tabs.

    Standalone tool for inspecting tab content without LLM calls.
    Filter by domain/title substring, or get top-engagement tabs.

    Args:
        tab_filter: Domain or title substring to match (e.g., "github", "obsidian").
            If not set, returns top-engagement tabs.
        tab_limit: Max tabs to extract (default 5).
        max_chars: Max characters per tab (default 5000).
    """
    from work_buddy.collectors.chrome_collector import request_content
    from work_buddy.collectors.chrome_infer import _resolve_tab_ids
    from work_buddy.collectors.chrome_ledger import (
        _is_noise_url,
        _read_ledger,
    )

    snapshots = _read_ledger()
    if not snapshots:
        return "No Chrome tab data available (ledger empty)."

    latest = snapshots[-1]
    tabs = [
        t for t in latest.get("tabs", [])
        if not _is_noise_url(t.get("url", ""))
    ]

    if not tabs:
        return "No tabs found in latest snapshot."

    # Filter
    if tab_filter:
        filt = tab_filter.lower()
        tabs = [
            t for t in tabs
            if filt in t.get("url", "").lower() or filt in t.get("title", "").lower()
        ]
        if not tabs:
            return f"No tabs matching '{tab_filter}'."

    tabs = tabs[:tab_limit]

    # Resolve tab IDs and extract
    urls = [t.get("url", "") for t in tabs]
    url_to_tid = _resolve_tab_ids(urls)

    tab_ids = []
    tid_to_tab: dict[int, dict] = {}
    for t in tabs:
        tid = url_to_tid.get(t.get("url", ""))
        if tid is not None:
            tab_ids.append(tid)
            tid_to_tab[tid] = t

    if not tab_ids:
        return "Could not resolve tab IDs (tabs may have changed)."

    results = request_content(tab_ids=tab_ids, max_chars=max_chars)
    if results is None:
        return "Chrome extension did not respond (is it running?)."

    lines = [f"## Page Content ({len(results)} tabs)\n"]
    for r in results:
        tid = r.get("tabId")
        tab = tid_to_tab.get(tid, {})
        title = tab.get("title", r.get("title", "Unknown"))
        url = tab.get("url", r.get("url", ""))

        if r.get("error"):
            lines.append(f"### {title}")
            lines.append(f"URL: {url}")
            lines.append(f"*Extraction failed: {r['error']}*\n")
        else:
            text = r.get("text", "")[:max_chars]
            lines.append(f"### {title}")
            lines.append(f"URL: {url}")
            lines.append(f"```\n{text}\n```\n")

    return "\n".join(lines)


def get_messages_context() -> str:
    """Inter-agent messaging state: pending, recent, unread messages."""
    from work_buddy.collectors import message_collector

    cfg = _cfg_with_overrides()
    return message_collector.collect(cfg)


def get_smart_context() -> str:
    """Smart Connections context: semantically related notes to active contracts."""
    from work_buddy.collectors import smart_collector

    cfg = _cfg_with_overrides()
    return smart_collector.collect(cfg)


def get_calendar_context(*, date: str | None = None, check_ready: bool = False) -> str:
    """Google Calendar schedule for a given date.

    Args:
        date: Date to show schedule for (YYYY-MM-DD). Default: today.
        check_ready: If true, return only a readiness check (no schedule fetch).
    """
    if check_ready:
        try:
            from work_buddy.calendar import check_ready as _check_ready
            return _check_ready()
        except Exception as exc:
            return f'{{"available": false, "reason": "{exc}"}}'

    from work_buddy.collectors import calendar_collector

    overrides: dict[str, Any] = {}
    if date:
        overrides["since"] = f"{date}T00:00:00"
        overrides["until"] = f"{date}T23:59:59"
    cfg = _cfg_with_overrides(**overrides)
    return calendar_collector.collect(cfg)


def get_projects_context() -> str:
    """Active projects with identity, state, and trajectory.

    Synthesizes project inventory from vault directories, STATE.md files
    in repos, task project tags, git activity, and contracts.
    Also syncs results to the project store.
    """
    from work_buddy.collectors import project_collector

    cfg = _cfg_with_overrides()
    return project_collector.collect(cfg)


# ---------------------------------------------------------------------------
# Project CRUD (backed by projects.store)
# ---------------------------------------------------------------------------

def project_list(*, status: str | None = None) -> str:
    """List all projects, optionally filtered by status."""
    import json
    from work_buddy.projects import store
    projects = store.list_projects(status=status)
    return json.dumps(projects, indent=2)


def project_get(*, slug: str) -> str:
    """Get a single project with recent observations (identity + state + trajectory).

    Returns the identity from the SQLite registry plus a recall from
    the Hindsight project memory bank for recent context.
    """
    import json
    from work_buddy.projects import store

    result = store.get_project(slug)
    if result is None:
        return json.dumps({"error": f"Project '{slug}' not found"})

    # Enrich with Hindsight recall (cheap embedding search)
    try:
        from work_buddy.memory.query import recall_project_context
        memory = recall_project_context(
            query=f"Current state, recent decisions, and trajectory for {slug}",
            project_slug=slug,
            budget="low",
            max_tokens=2048,
        )
        result["memory"] = memory if memory else None
    except Exception:
        result["memory"] = None

    # Remove SQLite observations from response (legacy, may still exist)
    result.pop("observations", None)

    return json.dumps(result, indent=2, default=str)


def project_observe(*, project: str, content: str) -> str:
    """Record an observation about a project into the project memory bank.

    Use this to capture chat-sourced intelligence: strategic decisions,
    supervisor feedback, pivots, blockers, or anything that shapes the
    project's trajectory but wouldn't appear in code or task lists.

    Observations are retained into Hindsight for LLM-powered extraction
    and semantic search.  The project identity registry is also touched
    to reflect recent activity.

    Args:
        project: Project slug (e.g. 'my-project', 'work-buddy').
        content: The observation — what happened, what it means,
            what changed.
    """
    import json
    import os
    from work_buddy.projects import store
    from work_buddy.memory.ingest import retain_project_observation

    session_id = os.environ.get("WORK_BUDDY_SESSION_ID")

    # Verify project exists in the registry (don't auto-create — use project_create)
    existing = store.get_project(project)
    if not existing:
        return json.dumps({
            "error": f"Project '{project}' not found in registry. Use project_create to add it first, or project_discover to find candidates.",
        }, indent=2)

    # Touch updated_at on the registry record
    store.touch_project(project)

    # Retain into Hindsight project memory bank
    result = retain_project_observation(
        project_slug=project,
        content=content,
        source="chat",
        session_id=session_id,
    )

    return json.dumps({
        "project": project,
        "retained": result is not None,
        "content_preview": content[:200],
    }, indent=2)


def project_update(
    *,
    slug: str,
    name: str | None = None,
    status: str | None = None,
    description: str | None = None,
) -> str:
    """Update a project's identity fields (name, status, description)."""
    import json
    from work_buddy.projects import store

    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if status is not None:
        kwargs["status"] = status
    if description is not None:
        kwargs["description"] = description

    if not kwargs:
        return json.dumps({"error": "No fields to update"})

    result = store.update_project(slug, **kwargs)
    if result is None:
        return json.dumps({"error": f"Project '{slug}' not found"})
    return json.dumps(result, indent=2)


def project_create(
    *,
    slug: str,
    name: str,
    status: str = "active",
    description: str | None = None,
) -> str:
    """Manually create a project. Consent-gated.

    Args:
        slug: Unique identifier (lowercase, hyphens).
        name: Human-readable project name.
        status: One of: active, paused, past, future, inferred.
        description: What is this project? (embeddable text).
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.projects import store

    if not consent_cache.is_granted("project_create"):
        import secrets
        raise ConsentRequired(
            operation="project_create",
            reason=f"Create project '{slug}' ({name}) with status={status}.",
            risk="low",
            token=secrets.token_hex(4),
            default_ttl=30,
        )

    result = store.upsert_project(slug, name, status=status, description=description)
    return json.dumps(result, indent=2)


def project_memory(
    *,
    query: str = "",
    mode: str = "search",
    model_id: str = "project-landscape",
    project: str | None = None,
    budget: str = "mid",
) -> str:
    """Read from the project memory bank (Hindsight-backed).

    Args:
        query: Search query for project memories.
        mode: "search" (semantic recall), "model" (mental model), "recent" (latest).
        model_id: For mode=model — project-landscape, active-risks,
            recent-decisions, inter-project-deps.
        project: Project slug to scope search. Omit for cross-project.
        budget: Retrieval depth: low (fast), mid, high (thorough).
    """
    import json
    from work_buddy.memory.query import project_memory_read

    result = project_memory_read(
        query=query, mode=mode, model_id=model_id,
        project=project, budget=budget,
    )
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2, default=str)


def project_discover() -> str:
    """Discover project candidates from signals not yet in the registry.

    Scans task tags, git repos, and chat sessions for project-shaped signals
    that don't match any confirmed project.  Returns candidates for agent
    review — the agent should evaluate each and use project_create to
    promote real projects.
    """
    import json
    from work_buddy.collectors import project_collector
    from work_buddy.projects import store

    cfg = _cfg_with_overrides()
    vault_root = __import__("pathlib").Path(cfg["vault_root"])
    repos_root = __import__("pathlib").Path(cfg.get("repos_root", vault_root / "repos"))
    git_days = cfg.get("git", {}).get("detail_days", 7)

    # Gather signals
    task_counts = project_collector._scan_task_projects(vault_root)
    git_activity = project_collector._scan_git_activity(repos_root, days=git_days)

    # Get confirmed slugs
    confirmed = {p["slug"] for p in store.list_projects()}

    # Also get alias targets so aliased slugs don't show as candidates
    aliases = cfg.get("projects", {}).get("aliases", {})
    aliased_slugs = set(aliases.keys())

    candidates = []
    seen = set()

    for slug, counts in task_counts.items():
        resolved = aliases.get(slug, slug)
        if resolved in confirmed or slug in aliased_slugs or slug in seen:
            continue
        seen.add(slug)
        candidates.append({
            "slug": slug,
            "sources": ["task_tags"],
            "tasks_open": counts.get("open", 0),
            "tasks_done": counts.get("done", 0),
        })

    for slug, activity in git_activity.items():
        resolved = aliases.get(slug, slug)
        if resolved in confirmed or slug in aliased_slugs or slug in seen:
            continue
        seen.add(slug)
        candidates.append({
            "slug": slug,
            "sources": ["git"],
            "recent_commits": activity.get("recent_commits", 0),
            "last_commit": activity.get("last_commit_date", "")[:10] if activity.get("last_commit_date") else None,
        })

    return json.dumps({
        "candidates": candidates,
        "confirmed_count": len(confirmed),
        "candidate_count": len(candidates),
    }, indent=2)


def project_delete(*, slug: str) -> str:
    """Delete a project from the identity registry. Consent-gated.

    This removes the project from the SQLite registry. Hindsight memories
    tagged with this project are NOT deleted (use memory_prune for that).
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.projects import store

    # Verify project exists
    existing = store.get_project(slug)
    if not existing:
        return json.dumps({"error": f"Project '{slug}' not found"})

    # Require consent
    if not consent_cache.is_granted("project_delete"):
        import secrets
        raise ConsentRequired(
            operation="project_delete",
            reason=f"Permanently delete project '{slug}' ({existing.get('name', slug)}) from the identity registry.",
            risk="moderate",
            token=secrets.token_hex(4),
            default_ttl=5,
        )

    deleted = store.delete_project(slug)
    return json.dumps({
        "deleted": deleted,
        "slug": slug,
        "note": "Hindsight memories for this project are preserved. Use memory_prune to remove them if needed.",
    }, indent=2)


# ---------------------------------------------------------------------------
# Bundle: full collection
# ---------------------------------------------------------------------------

def collect_bundle(
    *,
    days: int | None = None,
    hours: int | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Run all (or selected) collectors and save a context bundle to disk.

    Returns the bundle path and list of collectors run. Use the individual
    collector capabilities (``git_context``, ``chat_context``, etc.) when you
    only need one source — this is for full snapshots.

    Args:
        days: Override all time windows to N days.
        hours: Override all time windows to N hours (takes precedence over days).
        only: Comma-separated collector names to run (e.g. "git,chats").
            Valid names: git, obsidian, chats, chrome, messages, smart, calendar.
            Default: run all.
    """
    from work_buddy.collect import run_collection
    from work_buddy.config import load_config

    cfg = load_config()

    # Apply global time override (same logic as CLI's _expand_overrides)
    if hours is not None or days is not None:
        from work_buddy.collect import _TIME_TARGETS
        raw_days = (hours / 24.0) if hours is not None else float(days)
        for dotpath, coerce in _TIME_TARGETS:
            parts = dotpath.split(".")
            d = cfg
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = coerce(raw_days) if callable(coerce) else raw_days

    only_collector = None
    if only:
        # run_collection accepts a single --only, but we can call it per-collector
        # For simplicity, if only one is specified, pass it through
        collectors = [c.strip() for c in only.split(",")]
        if len(collectors) == 1:
            only_collector = collectors[0]
        else:
            # Multiple: run full collection but filter in the function
            # For now, just run all — the overhead is minimal
            only_collector = None

    bundle_path = run_collection(cfg, only=only_collector)
    return {
        "bundle_path": bundle_path.as_posix() if bundle_path else None,
        "message": f"Context bundle saved to {bundle_path}",
    }


# ---------------------------------------------------------------------------
# Activity timeline
# ---------------------------------------------------------------------------


def activity_timeline(
    *,
    since: str,
    until: str | None = None,
    deep: bool = False,
    target_date: str | None = None,
) -> str:
    """Infer recent activity and return a formatted timeline.

    Args:
        since: ISO datetime or relative shorthand (``2h``, ``1d``, ``30m``).
        until: ISO datetime. Default: now.
        deep: Also collect git/chat/vault signals (default: false).
        target_date: Journal date YYYY-MM-DD (default: inferred from since).
    """
    import re as _re
    from datetime import timedelta

    from work_buddy.activity import format_timeline, infer_activity
    from work_buddy.journal import user_now

    # Parse relative since values like "2h", "1d", "30m"
    rel_match = _re.fullmatch(r"(\d+)\s*(m|min|h|hour|hours|d|day|days)", since.strip())
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2)[0]  # first char: m, h, or d
        deltas = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}
        since_dt = user_now().replace(tzinfo=None) - deltas[unit]
        since = since_dt.isoformat()

    timeline = infer_activity(
        since=since,
        until=until,
        deep=deep,
        target_date=target_date,
    )
    return format_timeline(timeline)


# ---------------------------------------------------------------------------
# Hot files (fused from vault ledger + KTR)
# ---------------------------------------------------------------------------


def hot_files(
    *,
    since: str,
    sub_directory: str | None = None,
    collapse_threshold: int = 5,
) -> str:
    """Rank vault files by activity, with hierarchical directory aggregation.

    Fuses vault event ledger data (modify frequency) with KTR writing
    intensity (word deltas, editing sessions) into a single score per file.
    When many files cluster under one directory, collapses them into a
    directory summary to prevent context flooding.

    Args:
        since: Relative shorthand (``7d``, ``2h``) or ISO date (``2026-04-01``).
        sub_directory: Optional vault-relative path prefix to drill into
            (e.g. ``repos/work-buddy/work_buddy``). Shows full file-level
            granularity under this directory.
        collapse_threshold: Max files to show individually per directory
            before collapsing into a directory summary (default 5).
    """
    import re as _re
    from collections import defaultdict
    from datetime import timedelta

    from work_buddy.journal import user_now

    now = user_now().replace(tzinfo=None)

    # Parse since into a date string
    rel_match = _re.fullmatch(r"(\d+)\s*(m|min|h|hour|hours|d|day|days)", since.strip())
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2)[0]
        deltas = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}
        since_date = (now - deltas[unit]).strftime("%Y-%m-%d")
    elif len(since.strip()) == 10:
        since_date = since.strip()
    else:
        since_date = since[:10]

    until_date = now.strftime("%Y-%m-%d")

    # Gather files from both sources, filter junk
    scored_files = _gather_hot_files(since_date, until_date, sub_directory)
    scored_files = [f for f in scored_files if not _is_junk_file(f["path"])]

    if not scored_files:
        scope = f" under `{sub_directory}`" if sub_directory else ""
        return f"No vault file activity found since {since_date}{scope}."

    # Apply hierarchical aggregation
    if sub_directory:
        # Drill-down mode: show all files, no collapsing
        scored_files.sort(key=lambda f: f["score"], reverse=True)
        lines = [f"## Hot Files: `{sub_directory}/` (since {since_date})"]
        lines.append(f"**{len(scored_files)} files**\n")
        for f in scored_files:
            lines.append(_format_file_line(f))
        return "\n".join(lines)

    # Aggregate mode: collapse directories with many hot files
    tree = _build_hot_tree(scored_files, collapse_threshold)
    lines = [f"## Hot Files (since {since_date})", ""]
    for node in tree:
        if node["type"] == "directory":
            lines.append(
                f"- **`{node['path']}/`** ({node['file_count']} files, "
                f"score {node['total_score']:.0f}, "
                f"top: `{node['top_file']}`)"
            )
        else:
            lines.append(_format_file_line(node))

    lines.append(f"\n**Total:** {len(scored_files)} files with activity")
    return "\n".join(lines)


def _gather_hot_files(
    since_date: str, until_date: str, sub_directory: str | None,
) -> list[dict]:
    """Fuse vault ledger + KTR data into a single scored file list."""
    files: dict[str, dict] = {}

    # Source 1: vault event ledger
    try:
        from work_buddy.obsidian.vault_events import bootstrap, get_hot_files as ledger_hot
        bootstrap()
        exclude = [
            "journal", "agents", ".obsidian", ".specstory", "recovery-codes",
            # Also exclude agent dirs nested under repos
            "repos/work-buddy/agents",
        ]
        result = ledger_hot(since_date, until_date, limit=500, exclude_folders=exclude)
        for f in result.get("files", []):
            path = f["path"]
            files[path] = {
                "path": path,
                "score": f.get("hot_score", 0),
                "mods": f.get("total_modifications", 0),
                "active_days": f.get("active_days", 0),
                "last_modified": f.get("last_modified", ""),
                "words": 0,
                "writing_buckets": 0,
            }
    except Exception:
        pass

    # Source 2: KTR writing intensity
    try:
        from work_buddy.obsidian.ktr import get_hot_files as ktr_hot
        result = ktr_hot(since_date, until_date, limit=500)
        skip_segments = ("journal", "agents", ".obsidian", ".specstory", "recovery-codes")
        for f in result.get("files", []):
            path = f["filePath"]
            if any(seg + "/" in path or path.startswith(seg + "/") for seg in skip_segments):
                continue
            if path not in files:
                files[path] = {
                    "path": path,
                    "score": 0,
                    "mods": 0,
                    "active_days": 0,
                    "last_modified": "",
                    "words": 0,
                    "writing_buckets": 0,
                }
            entry = files[path]
            entry["words"] = f.get("total_word_delta", 0)
            entry["writing_buckets"] = f.get("total_buckets", 0)
            # Fuse scores: add KTR intensity to ledger frequency
            entry["score"] += f.get("hot_score", 0) * 0.5
    except Exception:
        pass

    result_list = list(files.values())

    # Filter by sub_directory
    if sub_directory:
        prefix = sub_directory.rstrip("/") + "/"
        result_list = [f for f in result_list if f["path"].startswith(prefix)]

    return result_list


def _build_hot_tree(
    files: list[dict], threshold: int,
) -> list[dict]:
    """Build hierarchical view, collapsing busy directories.

    Bottom-up: groups files by parent directory, collapses when count
    exceeds threshold, then repeats upward. Stops before collapsing
    into the vault root or depth-0 paths.
    """
    from collections import defaultdict

    # Start with all files as individual nodes
    nodes: list[dict] = [{"type": "file", **f} for f in files]

    # Iteratively collapse from the deepest level upward
    for _round in range(6):  # max 6 levels of nesting
        by_dir: dict[str, list[int]] = defaultdict(list)
        for i, node in enumerate(nodes):
            path = node["path"]
            parts = path.split("/")
            parent = "/".join(parts[:-1]) if len(parts) > 1 else ""
            if parent:
                by_dir[parent].append(i)

        # Find directories to collapse this round
        to_collapse: dict[str, list[int]] = {}
        for dir_path, indices in by_dir.items():
            if len(indices) > threshold:
                to_collapse[dir_path] = indices

        if not to_collapse:
            break

        # Build new node list: collapsed dirs + untouched nodes
        collapsed_indices: set[int] = set()
        new_dirs: list[dict] = []
        for dir_path, indices in to_collapse.items():
            child_nodes = [nodes[i] for i in indices]
            total_files = sum(
                n.get("file_count", 1) for n in child_nodes
            )
            total_score = sum(
                n.get("total_score", n.get("score", 0)) for n in child_nodes
            )
            max_days = max(
                (n.get("active_days", 0) for n in child_nodes), default=0
            )
            top = max(child_nodes, key=lambda n: n.get("total_score", n.get("score", 0)))
            top_file = top.get("top_file", top["path"].split("/")[-1])

            new_dirs.append({
                "type": "directory",
                "path": dir_path,
                "file_count": total_files,
                "total_score": total_score,
                "active_days": max_days,
                "top_file": top_file,
            })
            collapsed_indices.update(indices)

        nodes = [n for i, n in enumerate(nodes) if i not in collapsed_indices]
        nodes.extend(new_dirs)

    nodes.sort(key=lambda n: n.get("total_score", n.get("score", 0)), reverse=True)
    return nodes


def _is_junk_file(path: str) -> bool:
    """Filter out binary, cache, temp, and log files."""
    from work_buddy.activity import _is_junk_path
    return _is_junk_path(path)


def _format_file_line(f: dict) -> str:
    """Format a single file entry for display."""
    parts = [f"- `{f['path']}`"]
    details = []
    if f.get("score"):
        details.append(f"score {f['score']:.0f}")
    if f.get("mods"):
        details.append(f"{f['mods']} mods")
    if f.get("words"):
        details.append(f"{f['words']}w written")
    if f.get("active_days"):
        details.append(f"{f['active_days']}d")
    if details:
        parts.append(f"({', '.join(details)})")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Journal sign-in (composite: extract + wellness + write)
# ---------------------------------------------------------------------------


def journal_sign_in(
    *,
    target: str | None = None,
    write_fields: str | None = None,
) -> dict[str, Any]:
    """Read sign-in state and wellness trends, optionally write fields.

    Composite capability: replaces the need to call extract_sign_in(),
    interpret_wellness(), and write_sign_in() separately.

    Args:
        target: Date target — ``today``, ``yesterday``, or ``YYYY-MM-DD``.
            Default: today.
        write_fields: JSON dict of fields to write (e.g.
            ``{"sleep": 7, "energy": 8, "mood": 7, "check_in": "...", "motto": "..."}``)
            Only non-null fields are written. Consent-gated (``morning.write_sign_in``).
            Omit to read-only.
    """
    import json as _json
    from pathlib import Path

    from work_buddy.config import load_config
    from work_buddy.journal import (
        extract_sign_in,
        interpret_wellness,
        resolve_target_date,
        write_sign_in,
    )

    cfg = load_config()
    resolved = resolve_target_date(target)
    vault_root = Path(cfg["vault_root"])
    journal_path = vault_root / "journal" / f"{resolved.date}.md"

    result: dict[str, Any] = {
        "target_date": str(resolved.date),
        "sign_in": extract_sign_in(journal_path),
        "wellness": interpret_wellness(cfg),
    }

    if write_fields:
        fields = _json.loads(write_fields) if isinstance(write_fields, str) else write_fields
        write_result = write_sign_in(journal_path, fields)
        result["write_result"] = write_result
        # Re-read after write
        result["sign_in"] = extract_sign_in(journal_path)

    return result


# ---------------------------------------------------------------------------
# Journal write (composite: log entries or briefing)
# ---------------------------------------------------------------------------


def journal_write(
    *,
    mode: str = "log_entries",
    target: str | None = None,
    entries: str | None = None,
    briefing_md: str | None = None,
) -> dict[str, Any]:
    """Append log entries or persist a briefing to the journal.

    Args:
        mode: ``log_entries`` (default) or ``briefing``.
        target: Date target — ``today``, ``yesterday``, or ``YYYY-MM-DD``.
        entries: For ``log_entries`` mode: JSON list of ``[time, description]``
            tuples (e.g. ``[["2:15 PM", "Started coding"], ["4:00 PM", "Done"]]``).
            Formatting and tag insertion handled automatically.
        briefing_md: For ``briefing`` mode: markdown string to wrap in a
            briefing callout and insert in the Sign-In section.
    """
    import json as _json
    from pathlib import Path

    from work_buddy.config import load_config
    from work_buddy.journal import (
        append_to_journal,
        persist_briefing_to_journal,
        resolve_target_date,
    )

    cfg = load_config()
    resolved = resolve_target_date(target)
    vault_root = Path(cfg["vault_root"])
    date_str = str(resolved.date)

    if mode == "log_entries":
        if not entries:
            return {"error": "entries parameter required for log_entries mode"}
        parsed = _json.loads(entries) if isinstance(entries, str) else entries
        return append_to_journal(
            entries=parsed,
            vault_root=vault_root,
            date_str=date_str,
        )
    elif mode == "briefing":
        if not briefing_md:
            return {"error": "briefing_md parameter required for briefing mode"}
        return persist_briefing_to_journal(
            briefing_md=briefing_md,
            vault_root=vault_root,
            date_str=date_str,
        )
    else:
        return {"error": f"Unknown mode: {mode}. Use 'log_entries' or 'briefing'."}


# ---------------------------------------------------------------------------
# Day Planner (composite: status / read / generate / write)
# ---------------------------------------------------------------------------


def day_planner(
    *,
    action: str = "status",
    target: str | None = None,
    calendar_events: str | None = None,
    focused_tasks: str | None = None,
    config_overrides: str | None = None,
) -> dict[str, Any]:
    """Day Planner operations: check status, read plan, generate, or write.

    Args:
        action: ``status`` (check plugin readiness), ``read`` (get current plan),
            ``generate`` (create schedule from events+tasks), ``write`` (write
            generated entries to journal), or ``generate_and_write`` (both).
        target: Date target for read/write — ``today`` or ``YYYY-MM-DD``.
        calendar_events: For generate: JSON list of calendar event dicts
            (from ``context_calendar``).
        focused_tasks: For generate: JSON list of task dicts with ``description`` key.
        config_overrides: JSON dict of day_planner config overrides
            (work_hours, default_task_duration, break_interval, etc.).
    """
    import json as _json

    from work_buddy.journal import resolve_target_date

    if action == "status":
        from work_buddy.obsidian.day_planner import check_ready
        return check_ready()

    resolved = resolve_target_date(target)
    journal_path = f"journal/{resolved.date}.md"

    if action == "read":
        from work_buddy.obsidian.day_planner import get_todays_plan
        return get_todays_plan(journal_path)

    if action in ("generate", "generate_and_write"):
        from work_buddy.obsidian.day_planner import generate_plan

        events = _json.loads(calendar_events) if isinstance(calendar_events, str) else (calendar_events or [])
        tasks = _json.loads(focused_tasks) if isinstance(focused_tasks, str) else (focused_tasks or [])
        cfg = _json.loads(config_overrides) if isinstance(config_overrides, str) else (config_overrides or {})

        entries = generate_plan(events, tasks, cfg)
        result: dict[str, Any] = {
            "entries": entries,
            "entry_count": len(entries),
        }

        if action == "generate_and_write":
            from work_buddy.obsidian.day_planner import trigger_resync, write_plan
            write_result = write_plan(journal_path, entries)
            trigger_resync()
            result["write_result"] = write_result
            result["resynced"] = True

        return result

    if action == "write":
        # Write pre-generated entries (passed as focused_tasks for convenience)
        from work_buddy.obsidian.day_planner import trigger_resync, write_plan
        entries_to_write = _json.loads(focused_tasks) if isinstance(focused_tasks, str) else (focused_tasks or [])
        write_result = write_plan(journal_path, entries_to_write)
        trigger_resync()
        return {"write_result": write_result, "resynced": True}

    return {"error": f"Unknown action: {action}. Use status/read/generate/write/generate_and_write."}


# ── Chrome tab mutations ──────────────────────────────────────────


def triage_execute(
    decisions: dict,
    presentation: dict,
) -> dict:
    """Execute triage decisions from the review view."""
    from work_buddy.triage.execute import execute_triage_decisions
    return execute_triage_decisions(decisions, presentation)


def chrome_tab_close(tab_ids: list) -> dict:
    """Close specified Chrome tabs."""
    from work_buddy.collectors.chrome_collector import close_tabs

    if not tab_ids:
        return {"error": "No tab_ids provided"}

    int_ids = [int(t) for t in tab_ids]
    result = close_tabs(int_ids)
    if result is None:
        return {"error": "Chrome extension did not respond. Is it running?"}
    return result


def chrome_tab_group(
    tab_ids: list,
    title: str = "",
    color: str = "grey",
    group_id: int | None = None,
) -> dict:
    """Create or update a Chrome tab group."""
    from work_buddy.collectors.chrome_collector import group_tabs

    if not tab_ids:
        return {"error": "No tab_ids provided"}

    int_ids = [int(t) for t in tab_ids]
    result = group_tabs(int_ids, title=title, color=color, group_id=group_id)
    if result is None:
        return {"error": "Chrome extension did not respond. Is it running?"}
    return result


def chrome_tab_move(
    tab_ids: list,
    index: int = -1,
    window_id: int | None = None,
) -> dict:
    """Move Chrome tabs to a specific position."""
    from work_buddy.collectors.chrome_collector import move_tabs

    if not tab_ids:
        return {"error": "No tab_ids provided"}

    int_ids = [int(t) for t in tab_ids]
    result = move_tabs(int_ids, index=index, window_id=window_id)
    if result is None:
        return {"error": "Chrome extension did not respond. Is it running?"}
    return result


# ---------------------------------------------------------------------------
# Datacore (structured vault query)
# ---------------------------------------------------------------------------


def datacore_status() -> dict:
    """Check if Datacore is installed, initialized, and queryable.

    Returns readiness status, version, index revision, and object type counts.
    """
    from work_buddy.obsidian.datacore.env import check_ready

    return check_ready()


def datacore_query(
    query: str,
    *,
    fields: str | None = None,
    limit: int = 50,
) -> dict:
    """Execute a Datacore query and return serialized results.

    Args:
        query: Datacore query string (e.g. '@page and path("journal")').
        fields: Comma-separated field names to include (e.g. '$path,$tags').
            Default: all standard fields.
        limit: Maximum results (default 50).
    """
    from work_buddy.obsidian.datacore.env import query as dc_query

    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    return dc_query(query, fields=field_list, limit=limit)


def datacore_fullquery(
    query: str,
    *,
    fields: str | None = None,
    limit: int = 50,
) -> dict:
    """Execute a Datacore fullquery with timing and revision metadata.

    Args:
        query: Datacore query string.
        fields: Comma-separated field names. Default: all.
        limit: Maximum results (default 50).
    """
    from work_buddy.obsidian.datacore.env import fullquery

    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    return fullquery(query, fields=field_list, limit=limit)


def datacore_validate(query: str) -> dict:
    """Validate a Datacore query string without executing it.

    Returns {valid: true/false} with parse error details if invalid.
    """
    from work_buddy.obsidian.datacore.env import validate_query

    return validate_query(query)


def datacore_get_page(path: str, *, fields: str | None = None) -> dict:
    """Get a single vault page by path with Datacore metadata.

    Args:
        path: Vault-relative path (e.g. 'journal/2026-04-09.md').
        fields: Comma-separated field names. Default: all.
    """
    from work_buddy.obsidian.datacore.env import get_page

    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None
    return get_page(path, fields=field_list)


def datacore_evaluate(
    expression: str,
    *,
    source_path: str | None = None,
) -> dict:
    """Evaluate a Datacore expression.

    Args:
        expression: Expression string (e.g. '1 + 2' or 'this.$tags').
        source_path: Optional vault path for 'this' context.
    """
    from work_buddy.obsidian.datacore.env import evaluate

    return evaluate(expression, source_path=source_path)


def datacore_schema() -> dict:
    """Summarize the vault's Datacore schema: object types, tags, frontmatter keys, path prefixes.

    Useful for understanding what's queryable before building a query plan.
    """
    from work_buddy.obsidian.datacore.env import schema_summary

    return schema_summary(sample_limit=200)


def datacore_compile_plan(plan_json: str) -> dict:
    """Compile a structured JSON query plan into a Datacore query string.

    The plan is a JSON object with keys like target, path, tags, status, child_of, etc.
    See work_buddy/obsidian/datacore/compiler.py for the full schema.

    Args:
        plan_json: JSON string of the query plan.

    Returns:
        Dict with 'query' (the compiled string) and 'valid' (whether Datacore accepts it).
    """
    import json as _json

    from work_buddy.obsidian.datacore.compiler import compile_plan, validate_plan, CompileError

    try:
        plan = _json.loads(plan_json)
    except _json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    warnings = validate_plan(plan)
    if warnings:
        return {"error": f"Plan validation failed: {'; '.join(warnings)}"}

    try:
        query_str = compile_plan(plan)
    except CompileError as e:
        return {"error": str(e)}

    # Validate against Datacore parser
    from work_buddy.obsidian.datacore.env import validate_query

    validation = validate_query(query_str)
    return {
        "query": query_str,
        "valid": validation.get("valid", False),
        "parse_error": validation.get("parse_error"),
    }


def datacore_run_plan(
    plan_json: str,
    *,
    fields: str | None = None,
    limit: int = 50,
) -> dict:
    """Compile and execute a structured query plan in one step.

    Combines compile_plan + query. Returns both the compiled query string
    and the results.

    Args:
        plan_json: JSON string of the query plan.
        fields: Comma-separated field names. Default: all.
        limit: Maximum results (default 50).
    """
    import json as _json

    from work_buddy.obsidian.datacore.compiler import compile_plan, validate_plan, CompileError
    from work_buddy.obsidian.datacore.env import query as dc_query

    try:
        plan = _json.loads(plan_json)
    except _json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    warnings = validate_plan(plan)
    if warnings:
        return {"error": f"Plan validation failed: {'; '.join(warnings)}"}

    try:
        query_str = compile_plan(plan)
    except CompileError as e:
        return {"error": str(e)}

    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else None

    result = dc_query(query_str, fields=field_list, limit=limit)
    result["compiled_query"] = query_str
    return result


# ---------------------------------------------------------------------------
# Scattered tasks (Datacore-powered task discovery)
# ---------------------------------------------------------------------------


def task_scattered(*, limit: int = 200) -> dict:
    """Find open tasks scattered across the vault outside the master task list.

    Uses Datacore to query all open tasks, then separates master-list tasks
    from those embedded in journal entries, project docs, READMEs, etc.
    Groups scattered tasks by file with counts.

    Args:
        limit: Maximum scattered tasks to return (default 200).

    Returns:
        Dict with master_count, scattered_count, by_file (grouped results),
        and files_count.
    """
    from work_buddy.obsidian.datacore.env import query as dc_query, fullquery

    # Get total open task count and master list count separately
    totals = fullquery('@task and $status = " "', fields=["$file"], limit=0)
    total = totals.get("total", 0)

    master_list = "tasks/master-task-list.md"
    master_result = fullquery(
        f'@task and $status = " " and $file = "{master_list}"',
        fields=["$file"],
        limit=0,
    )
    master_count = master_result.get("total", 0)

    # Query scattered tasks (exclude master list via $file negation)
    scattered_result = dc_query(
        f'@task and $status = " " and !($file = "{master_list}")',
        fields=["$text", "$file", "$tags"],
        limit=limit,
    )

    tasks = scattered_result.get("results", [])
    scattered_total = scattered_result.get("total", 0)

    # Group by file
    by_file: dict[str, list[dict]] = {}
    for t in tasks:
        f = t.get("$file", "unknown")
        by_file.setdefault(f, []).append(t)

    # Sort by count descending
    grouped = [
        {
            "file": filepath,
            "count": len(file_tasks),
            "tasks": [
                {
                    "text": t.get("$text", "").strip()[:120],
                    "tags": t.get("$tags", []),
                }
                for t in file_tasks
            ],
        }
        for filepath, file_tasks in sorted(
            by_file.items(), key=lambda x: -len(x[1])
        )
    ]

    return {
        "total_open": total,
        "master_count": master_count,
        "scattered_count": scattered_total,
        "scattered_returned": len(tasks),
        "files_count": len(by_file),
        "by_file": grouped,
    }
