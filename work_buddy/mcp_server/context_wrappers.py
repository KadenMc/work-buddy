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

from work_buddy.obsidian.retry import bridge_retry


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

    Delegates to the multi-repo :class:`work_buddy.context.sources.git.GitSource`,
    which walks every ``.git`` directory under ``cfg['repos_root']``.

    Args:
        days: Lookback window for detailed commit history.
        dirty_only: If true, only include repos with uncommitted changes.
        annotate: If true, tag commits made by agent sessions with their
            session ID. Slower — scans JSONL session files.
    """
    from work_buddy.context.sources.git import GitSource
    from work_buddy.context.types import ContextRequest, ContextDepth

    custom: dict[str, Any] = {
        "dirty_only": dirty_only,
        "include_status": True,
        "max_commits": 100,
    }
    if annotate:
        from work_buddy.sessions.inspector import build_session_map
        custom["session_map"] = build_session_map(days=days)

    src = GitSource()
    request = ContextRequest(
        sources=["git"],
        window_days=days,
        depth=ContextDepth.DEEP,
        custom={"git": custom},
    )
    section = src.collect(request)
    return src.render(section, ContextDepth.DEEP)


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


def get_vault_context() -> str:
    """Vault context: contract-relevant notes via the native vault index."""
    from work_buddy.collectors import vault_collector

    cfg = _cfg_with_overrides()
    return vault_collector.collect(cfg)


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


def get_projects_context(*, statuses: list[str] | None = None) -> str:
    """Active projects with identity, state, and trajectory.

    Synthesizes project inventory from vault directories, STATE.md files
    in repos, task project tags, git activity, and contracts.
    Also syncs results to the project store.

    ``statuses`` filters only the rendered markdown — every project still
    gets scanned and synced. Default is active only (paused / future /
    past are hidden to keep bundles uncluttered); valid values are
    active, paused, future, past. Pass an explicit list to widen.
    ``deleted`` is never rendered through this surface.
    """
    from work_buddy.projects.sync import sync_projects

    cfg = _cfg_with_overrides()
    return sync_projects(cfg, statuses=statuses)


# ---------------------------------------------------------------------------
# Project CRUD (backed by projects.store)
# ---------------------------------------------------------------------------

def project_list(
    *, status: str | None = None, include_deleted: bool = False,
) -> str:
    """List all projects, optionally filtered by status.

    Rows with ``status='deleted'`` are filtered out by default; pass
    ``include_deleted=True`` to see them. Pass ``status='deleted'`` to
    see only the soft-deleted projects.
    """
    import json
    from work_buddy.projects import store
    projects = store.list_projects(status=status, include_deleted=include_deleted)
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
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Update a project's identity fields and append a revision.

    Resolves ``slug`` via the alias table, so a prior name (e.g.
    ``electricrag``) routes to the canonical row (``ecg-inquiry``).
    Writes a revision row capturing the post-update state — including
    the author (``user`` or ``agent``) and an optional one-line
    ``change_summary``. Agents should pass ``author='agent'``;
    user-initiated mutations should pass ``author='user'`` (default).
    """
    import json
    from work_buddy.projects import store

    kwargs: dict[str, Any] = {"author": author, "change_summary": change_summary}
    if name is not None:
        kwargs["name"] = name
    if status is not None:
        kwargs["status"] = status
    if description is not None:
        kwargs["description"] = description

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
    origin: str = "manual",
    folders: list[dict[str, Any]] | None = None,
    aliases: list[str] | None = None,
    author: str = "user",
) -> str:
    """Manually create a project. Consent-gated.

    Args:
        slug: Unique identifier (lowercase, hyphens).
        name: Human-readable project name.
        status: One of: ``active``, ``paused``, ``past``, ``future``.
            ``deleted`` is not creatable here — use ``project_delete``.
        description: What is this project? (free-form prose; versioned
            via the revision history).
        origin: ``manual`` (default — user/agent registered explicitly)
            or ``vault`` (auto-detected from a vault directory; usually
            set by the signal-scan, not by callers).
        folders: Optional list of ``{"path": str, "archived": bool}``
            dicts. Each path is an absolute system path; ``archived``
            marks a dormant location.
        aliases: Optional list of alternative slug strings. Stored
            with displayed casing preserved; matched case-insensitively
            for lookup.
        author: ``user`` (default) or ``agent``. Recorded on the
            initial revision row.
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.projects import store

    if not consent_cache.is_granted("project_create"):
        raise ConsentRequired(
            operation="project_create",
            reason=f"Create project '{slug}' ({name}) with status={status}.",
            risk="low",
            default_ttl=30,
        )

    folder_pairs: list[tuple[str, int]] | None = None
    if folders is not None:
        folder_pairs = [
            (f["path"], 1 if f.get("archived") else 0) for f in folders
        ]

    alias_pairs: list[tuple[str, str]] | None = None
    if aliases is not None:
        alias_pairs = [
            (a, store._normalize_slug(a)) for a in aliases if a.strip()
        ]

    result = store.upsert_project(
        slug, name,
        status=status,
        description=description,
        origin=origin,
        author=author,
        folders=folder_pairs,
        aliases=alias_pairs,
        change_summary="created",
    )
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

    Scans task tags and git repos for project-shaped signals that don't
    match any confirmed project (canonical slug or alias). Returns
    candidates for agent review — the agent evaluates each and uses
    ``project_create`` to promote real ones.
    """
    import json
    from work_buddy.projects import sync as project_sync
    from work_buddy.projects import store

    cfg = _cfg_with_overrides()
    vault_root = __import__("pathlib").Path(cfg["vault_root"])
    repos_root = __import__("pathlib").Path(cfg.get("repos_root", vault_root / "repos"))
    git_days = cfg.get("git", {}).get("detail_days", 7)

    task_counts = project_sync._scan_task_projects(vault_root)
    git_activity = project_sync._scan_git_activity(repos_root, days=git_days)

    # Any slug that resolves via the store (canonical or alias) is
    # NOT a candidate. ``resolve_slug`` returns None for unknown slugs.
    def is_known(slug: str) -> bool:
        try:
            return store.resolve_slug(slug) is not None
        except Exception:
            return False

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for slug, counts in task_counts.items():
        if slug in seen or is_known(slug):
            continue
        seen.add(slug)
        candidates.append({
            "slug": slug,
            "sources": ["task_tags"],
            "tasks_open": counts.get("open", 0),
            "tasks_done": counts.get("done", 0),
        })

    for slug, activity in git_activity.items():
        if slug in seen or is_known(slug):
            continue
        seen.add(slug)
        candidates.append({
            "slug": slug,
            "sources": ["git"],
            "recent_commits": activity.get("recent_commits", 0),
            "last_commit": (
                activity.get("last_commit_date", "")[:10]
                if activity.get("last_commit_date") else None
            ),
        })

    try:
        confirmed_count = len(store.list_projects(include_deleted=False))
    except Exception:
        confirmed_count = 0

    return json.dumps({
        "candidates": candidates,
        "confirmed_count": confirmed_count,
        "candidate_count": len(candidates),
    }, indent=2)


def project_delete(*, slug: str, author: str = "user") -> str:
    """Soft-delete a project (set ``status='deleted'``). Consent-gated.

    The row, its folders, its aliases, and its full revision history
    remain in the SQLite store. Default queries filter the row out;
    pass ``include_deleted=True`` to ``project_list`` to see it.
    Hindsight memories tagged with this project are preserved
    regardless — use ``memory_prune`` to remove them if desired.

    Hard-deletion is not exposed through this surface.
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.projects import store

    existing = store.get_project(slug)
    if not existing:
        return json.dumps({"error": f"Project '{slug}' not found"})

    if not consent_cache.is_granted("project_delete"):
        raise ConsentRequired(
            operation="project_delete",
            reason=(
                f"Soft-delete project '{slug}' "
                f"({existing.get('name', slug)}): set status='deleted'. "
                "Row + folders + aliases + history are preserved."
            ),
            risk="moderate",
            default_ttl=5,
        )

    deleted = store.delete_project(slug, author=author)
    return json.dumps({
        "deleted": deleted,
        "slug": slug,
        "note": (
            "Soft-deleted. Row preserved; status='deleted'. Revision "
            "history and Hindsight memories untouched."
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# Project folders / aliases / revisions
# ---------------------------------------------------------------------------


def _resolve_project_or_error(slug: str) -> tuple[int | None, str | None]:
    """Resolve ``slug`` (or alias) to a project_id. Returns (pid, error_json).

    On success: ``(pid, None)``. On not-found: ``(None, json_error_string)``.
    """
    import json
    from work_buddy.projects import store

    pid = store.resolve_slug(slug)
    if pid is None:
        return None, json.dumps({"error": f"Project '{slug}' not found"})
    return pid, None


def project_add_folder(
    *,
    slug: str,
    path: str,
    archived: bool = False,
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Attach a folder to a project. Writes a revision.

    ``path`` is an absolute system path. If it doesn't exist on disk a
    warning is logged but the row is still stored — allows for future
    paths, network drives, etc.
    """
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    result = store.add_folder(
        pid, path, archived=archived, author=author,
        change_summary=change_summary,
    )
    return json.dumps(result, indent=2)


def project_remove_folder(
    *,
    slug: str,
    path: str,
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Detach a folder from a project. Writes a revision."""
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    result = store.remove_folder(
        pid, path, author=author, change_summary=change_summary,
    )
    return json.dumps(result, indent=2)


def project_set_folder_archived(
    *,
    slug: str,
    path: str,
    archived: bool,
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Flip the ``archived`` flag on a folder. Writes a revision."""
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    result = store.set_folder_archived(
        pid, path, archived, author=author, change_summary=change_summary,
    )
    return json.dumps(result, indent=2)


def project_add_alias(
    *,
    slug: str,
    alias: str,
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Attach an alias to a project. Writes a revision.

    Raises ValueError if the alias collides with another project's
    canonical slug or alias.
    """
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    try:
        result = store.add_alias(
            pid, alias, author=author, change_summary=change_summary,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(result, indent=2)


def project_remove_alias(
    *,
    slug: str,
    alias: str,
    author: str = "user",
    change_summary: str | None = None,
) -> str:
    """Detach an alias from a project. Writes a revision."""
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    result = store.remove_alias(
        pid, alias, author=author, change_summary=change_summary,
    )
    return json.dumps(result, indent=2)


def project_confirm_description(*, slug: str) -> str:
    """Mark the latest revision as user-confirmed (records confirmation timestamp).

    Use this when a human reviews an LLM-authored description (or any
    other LLM-authored mutation) and signs off. The revision row's
    ``user_confirmed_at`` is set to the current time. Returns the
    revision id touched.
    """
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    revision_id = store.confirm_description(pid)
    if revision_id is None:
        return json.dumps({
            "error": f"Project '{slug}' has no revisions to confirm",
        })
    return json.dumps({
        "slug": slug,
        "project_id": pid,
        "confirmed_revision_id": revision_id,
    }, indent=2)


def project_revisions_list(*, slug: str, limit: int = 20) -> str:
    """Return revision history for a project, newest first.

    Each entry includes the snapshot of project fields plus the folder
    and alias sets as of that revision.
    """
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    rows = store.list_revisions(pid, limit=limit)
    return json.dumps(rows, indent=2, default=str)


def project_state_at(*, slug: str, timestamp: str) -> str:
    """Return the project's state as of ``timestamp`` (ISO 8601 UTC).

    Reconstructs the latest revision whose ``created_at <= timestamp``,
    with its folder and alias sets joined in. Returns an error JSON if
    no revision predates the given moment.
    """
    import json
    from work_buddy.projects import store

    pid, err = _resolve_project_or_error(slug)
    if err:
        return err
    assert pid is not None
    rev = store.get_state_at(pid, timestamp)
    if rev is None:
        return json.dumps({
            "error": f"No revision for '{slug}' at or before {timestamp}",
        })
    return json.dumps(rev, indent=2, default=str)


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
            Valid names: git, obsidian, chats, chrome, messages, vault, calendar.
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


@bridge_retry()
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


@bridge_retry()
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


@bridge_retry()
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
        calendar_events: For generate — JSON list of event dicts. Accepted shapes:

            Flat (recommended for manual construction):
                ``[{"start": "HH:MM", "end": "HH:MM", "summary": "..."},
                   {"start": "HH:MM", "end": "HH:MM", "description": "...", "past": false}]``
                ``start``/``end`` accept ``"HH:MM"``, ISO datetime, or minute-int.
                Label key: ``summary`` | ``description`` | ``text``. Set ``past: true`` to skip.

            Google Calendar API shape (raw events from the Calendar API):
                ``[{"start": {"dateTime": "2026-04-16T13:00:00-04:00"},
                    "end":   {"dateTime": "2026-04-16T13:30:00-04:00"},
                    "summary": "...", "timeStatus": "future"}]``
                All-day events (``start.date`` without ``start.dateTime``) and
                events with ``timeStatus == "past"`` are skipped.

        focused_tasks: For generate — JSON list of task dicts. Keys per task:

            - ``description`` or ``text`` (required): task label
            - ``duration`` (optional, int minutes): overrides config default
            - ``time_start`` (optional, ``"HH:MM"``): pin task to this start time;
              goes to unscheduled if it conflicts with a calendar slot or another
              pinned task.

        config_overrides: JSON dict of day_planner config overrides
            (``work_hours``, ``default_task_duration``, ``break_interval``,
            ``clamp_to_now`` — default True, prevents placement in the past).
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


def vault_recon(path_prefix: str | None = None, activity_days: int = 30) -> dict:
    """Diagnostic-grade vault reconnaissance: cross-tabs an agent can reason over.

    Returns frontmatter state machines (type x status), tag families (depth-3 tree),
    path-by-type distribution, recent activity by region, and cardinality-capped
    frontmatter values. Single page walk with anti-noise caps.

    Args:
        path_prefix: Optional vault-relative path prefix to scope the walk
            (e.g. "repos/electricrag/"). None = full vault.
        activity_days: Lookback window for recent_activity_by_path (default 30).
    """
    from work_buddy.obsidian.datacore.env import vault_recon as _vault_recon

    return _vault_recon(path_prefix=path_prefix, activity_days=activity_days)


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


# ---------------------------------------------------------------------------
# Entity registry (backed by entities.store)
# ---------------------------------------------------------------------------
#
# Federated resolution rides on a small provider protocol: each provider is
# a callable ``(query: str) -> list[dict]`` returning zero or more match
# dicts shaped like
#
#     {
#         "provider": "entities" | "projects" | ...,
#         "kind": "person" | "project" | ...,    # provider-defined
#         "id": <opaque ref into the provider>,
#         "name": <display>,
#         "description": <prose or None>,
#         "aliases": [<str>, ...],
#         "tags": [<str>, ...],                  # entities only
#     }
#
# ``entity_resolve`` calls every registered provider and merges the
# results. New providers (contracts, users, …) are added by extending
# ``_RESOLUTION_PROVIDERS`` below. The function never falls back from one
# provider to another — they're queried in parallel and all matches are
# returned. This is what makes "federated resolution" the accurate term
# (as opposed to "fallback").


def _entity_provider_entities(query: str) -> list[dict[str, Any]]:
    """Resolution provider: the entity store.

    Returns zero, one, or many matches. The entity store's
    ``resolve_name`` does canonical-then-alias lookup; we return the
    single match here (entities have a uniqueness constraint, so an
    alias lookup can return at most one entity).
    """
    from work_buddy.entities import store as entity_store

    eid = entity_store.resolve_name(query)
    if eid is None:
        return []
    e = entity_store.get_entity(eid)
    if e is None:
        return []
    return [{
        "provider": "entities",
        "kind": _entity_kind_from_tags(e.get("tags") or []),
        "id": e["id"],
        "name": e["canonical_name"],
        "description": e.get("description"),
        "aliases": [a["alias"] for a in e.get("aliases") or []],
        "tags": [t["tag"] for t in e.get("tags") or []],
    }]


def _entity_kind_from_tags(tags: list[dict[str, Any]]) -> str | None:
    """Derive a top-level kind string from an entity's tags.

    Returns the topmost path segment of the first tag (e.g. ``person``
    for ``person/family``). Useful for callers that want a quick
    "what is this" label without parsing the tag list. ``None`` if
    no tags.
    """
    for t in tags:
        norm = t.get("tag_norm") if isinstance(t, dict) else None
        if norm:
            return norm.split("/", 1)[0]
    return None


def _entity_provider_projects(query: str) -> list[dict[str, Any]]:
    """Resolution provider: the project registry.

    Projects already implement the entity-result shape: canonical
    ``slug``, ``aliases``, and ``description`` — so they qualify as a
    resolution source at zero migration cost. We do NOT copy project
    rows into the entity table; the project store remains sole owner
    of its rows.
    """
    from work_buddy.projects import store as project_store

    pid = project_store.resolve_slug(query)
    if pid is None:
        return []
    p = project_store.get_project_by_id(pid, include_deleted=False)
    if p is None:
        return []
    return [{
        "provider": "projects",
        "kind": "project",
        "id": p["slug"],
        "name": p.get("name") or p["slug"],
        "description": p.get("description"),
        "aliases": [a["alias"] for a in p.get("aliases") or []],
        "tags": [],
    }]


# Order matters only for tie-breaking when callers want a deterministic
# first match — the merge itself is order-stable. Registering a new
# provider is a one-line append below.
_RESOLUTION_PROVIDERS: list = [
    _entity_provider_entities,
    _entity_provider_projects,
]


def _maybe_record_resolution_reference(
    matches: list[dict[str, Any]],
    *,
    source_path: str | None,
    source_kind: str | None,
) -> None:
    """Side-effect: record an entity reference for each entities-provider hit.

    Only fires when the caller supplied both ``source_path`` and
    ``source_kind``. Matches from the projects provider (or any
    non-entities provider) are not recorded — references belong to the
    entity store, not to the project registry. Best-effort: a recording
    failure must not bubble up and break the resolve call.
    """
    if not source_path or not source_kind:
        return
    from work_buddy.entities import store as entity_store
    for m in matches:
        if m.get("provider") != "entities":
            continue
        try:
            entity_store.record_reference(
                entity_id=m["id"],
                source_path=source_path,
                source_kind=source_kind,
            )
        except Exception:
            # Logged at the store level; never break resolve.
            pass


def entity_resolve(
    *,
    query: str,
    source_path: str | None = None,
    source_kind: str | None = None,
) -> str:
    """Federated lookup over the entity store + the project registry.

    Returns all matches from every registered resolution provider. The
    federation is parallel (all providers are queried), not fallback —
    a name that's both an entity and a project name surfaces twice,
    flagged by ``provider``. Callers disambiguate.

    Args:
        query: A name, alias, or slug to resolve. Case-insensitive.
        source_path: Optional document/session/agent path. When
            supplied alongside ``source_kind``, each entities-provider
            match is recorded as a reference (de-dup window applies).
            Pass these together to populate the reference index from
            inside other agent work.
        source_kind: One of ``document``, ``chat``, ``task``,
            ``agent``, ``manual``. Required alongside ``source_path``
            for the side-effect reference recording.

    Returns:
        JSON string:
        ``{"query": ..., "matches": [...], "ambiguous": bool}``. The
        ``ambiguous`` flag is True when more than one match was found
        across all providers — the agent should disambiguate before
        acting.
    """
    import json

    matches: list[dict[str, Any]] = []
    for provider in _RESOLUTION_PROVIDERS:
        try:
            matches.extend(provider(query))
        except Exception:
            # A misbehaving provider must not poison the whole resolve.
            # The provider is responsible for its own logging.
            continue

    _maybe_record_resolution_reference(
        matches, source_path=source_path, source_kind=source_kind,
    )

    return json.dumps({
        "query": query,
        "matches": matches,
        "ambiguous": len(matches) > 1,
    }, indent=2, default=str)


def entity_list(
    *, tag: str | None = None, limit: int | None = None,
) -> str:
    """List entities, optionally filtered by a hierarchical tag.

    ``tag='person'`` returns everything tagged ``person`` plus
    ``person/family``, ``person/colleague``, etc. ``limit`` caps the
    result set; omit for everything.
    """
    import json
    from work_buddy.entities import store as entity_store

    entities = entity_store.list_entities(tag=tag, limit=limit)
    return json.dumps(entities, indent=2, default=str)


def entity_get(*, name_or_id: str) -> str:
    """Fetch a single entity by canonical name, alias, or integer id.

    Returns the full record including tags, aliases, and a recency
    snippet of the reference index (last 5 references). Returns
    ``{"error": ...}`` if not found.

    A numeric string (e.g. ``"2024"``) is resolved as a name/alias
    first and only falls back to an integer id lookup on a miss — so
    an entity that happens to be *named* a number is still reachable
    by name, while ``entity_get(name_or_id="7")`` still finds entity
    id 7 when no entity is named "7".
    """
    import json
    from work_buddy.entities import store as entity_store

    e = entity_store.get_entity(name_or_id)
    if e is None and isinstance(name_or_id, str) and name_or_id.isdigit():
        e = entity_store.get_entity(int(name_or_id))
    if e is None:
        return json.dumps({"error": f"Entity {name_or_id!r} not found"})
    e["recent_references"] = entity_store.list_references(e["id"], limit=5)
    e["reference_count"] = entity_store.count_references(e["id"])
    return json.dumps(e, indent=2, default=str)


def entity_create(
    *,
    canonical_name: str,
    description: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    author: str = "user",
    source_path: str | None = None,
    source_kind: str | None = None,
) -> str:
    """Create a new entity. Consent-gated for agent-author writes.

    User-author writes (``author='user'``, the default) skip the
    consent gate — the user invoking the capability is the consent.
    Agent-author writes raise ``ConsentRequired`` on first call so the
    user can approve the creation pattern; subsequent writes ride the
    cached grant within its TTL.

    If ``source_path`` and ``source_kind`` are both supplied, an
    initial reference row is appended for this entity recording the
    create event. This is how an agent that creates an entity while
    working in a document context anchors the entity to that document
    without a separate ``entity_add_reference`` call.
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.entities import store as entity_store

    if author == "agent" and not consent_cache.is_granted("entity_create"):
        raise ConsentRequired(
            operation="entity_create",
            reason=f"Create entity {canonical_name!r} with tags={tags or []}.",
            risk="low",
            default_ttl=30,
        )

    try:
        entity = entity_store.create_entity(
            canonical_name,
            description=description,
            tags=tags,
            aliases=aliases,
            author=author,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if source_path and source_kind:
        try:
            entity_store.record_reference(
                entity_id=entity["id"],
                source_path=source_path,
                source_kind=source_kind,
                snippet=f"created: {canonical_name}",
            )
        except ValueError:
            # Invalid source_kind — the create still succeeded, just
            # surface the failure as a field on the response so the
            # caller can fix the kind on retry.
            entity["reference_record_error"] = (
                f"invalid source_kind {source_kind!r}; entity created "
                "without an initial reference"
            )

    return json.dumps(entity, indent=2, default=str)


def entity_update(
    *,
    entity_id: int,
    canonical_name: str | None = None,
    description: str | None = None,
    author: str = "user",
    source_path: str | None = None,
    source_kind: str | None = None,
) -> str:
    """Update an entity's canonical name and/or description.

    Only provided fields change. Pass ``description=""`` (empty string)
    to clear the description; omit ``description`` to leave it alone.
    Tags and aliases are managed through ``entity_set_tags`` /
    ``entity_add_alias`` / ``entity_remove_alias`` — they don't appear
    here so a rename PATCH can't accidentally wipe them.

    If ``source_path`` and ``source_kind`` are supplied, an update
    reference is appended (subject to the standard de-dup window).
    """
    import json
    from work_buddy.entities import store as entity_store

    kwargs: dict[str, Any] = {"author": author}
    if canonical_name is not None:
        kwargs["canonical_name"] = canonical_name
    if description is not None:
        # An explicit empty string means "clear it." We translate to
        # None at the store boundary so the column is NULL, not "".
        kwargs["description"] = description if description != "" else None

    try:
        updated = entity_store.update_entity(entity_id, **kwargs)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if updated is None:
        return json.dumps({"error": f"Entity id={entity_id} not found"})

    if source_path and source_kind:
        try:
            entity_store.record_reference(
                entity_id=updated["id"],
                source_path=source_path,
                source_kind=source_kind,
                snippet=f"updated: {updated['canonical_name']}",
            )
        except ValueError:
            updated["reference_record_error"] = (
                f"invalid source_kind {source_kind!r}; update succeeded"
            )

    return json.dumps(updated, indent=2, default=str)


def entity_delete(
    *,
    entity_id: int,
    author: str = "user",
) -> str:
    """Hard-delete an entity. Consent-gated.

    Cascades through tags, aliases, and references. The handoff design
    chose hard-delete over soft-delete; if you need preservation, add
    a tag like ``status/archived`` and filter on it at read time.

    Both user-author and agent-author deletes go through the consent
    gate: deletion is destructive and the user should always see a
    prompt the first time per cache window.
    """
    import json
    from work_buddy.consent import ConsentRequired, _cache as consent_cache
    from work_buddy.entities import store as entity_store

    if not consent_cache.is_granted("entity_delete"):
        # Look up the entity name so the consent prompt is meaningful.
        existing = entity_store.get_entity(entity_id)
        name = existing["canonical_name"] if existing else f"id={entity_id}"
        ref_count = (
            entity_store.count_references(entity_id) if existing else 0
        )
        raise ConsentRequired(
            operation="entity_delete",
            reason=(
                f"Delete entity {name!r} (id={entity_id}). Cascades "
                f"through {ref_count} reference row(s)."
            ),
            risk="medium",
            default_ttl=15,
        )

    if not entity_store.delete_entity(entity_id, author=author):
        return json.dumps({"error": f"Entity id={entity_id} not found"})
    return json.dumps({"deleted": True, "entity_id": entity_id})


def entity_set_tags(
    *,
    entity_id: int,
    tags: list[str],
    author: str = "user",
) -> str:
    """Replace the full tag set on an entity.

    To remove all tags, pass an empty list. The store normalizes each
    tag (lowercase, collapse adjacent slashes) and de-duplicates the
    input before writing.
    """
    import json
    from work_buddy.entities import store as entity_store

    updated = entity_store.set_tags(entity_id, tags, author=author)
    if updated is None:
        return json.dumps({"error": f"Entity id={entity_id} not found"})
    return json.dumps(updated, indent=2, default=str)


def entity_add_alias(
    *,
    entity_id: int,
    alias: str,
    author: str = "user",
) -> str:
    """Attach an alias to an entity.

    Raises a wrapped error if the alias collides with another entity's
    canonical name or another entity's alias. An alias belongs to
    exactly one entity at a time.
    """
    import json
    from work_buddy.entities import store as entity_store

    try:
        updated = entity_store.add_alias(entity_id, alias, author=author)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if updated is None:
        return json.dumps({"error": f"Entity id={entity_id} not found"})
    return json.dumps(updated, indent=2, default=str)


def entity_remove_alias(
    *,
    entity_id: int,
    alias: str,
    author: str = "user",
) -> str:
    """Detach an alias from an entity. No-op if not attached."""
    import json
    from work_buddy.entities import store as entity_store

    updated = entity_store.remove_alias(entity_id, alias, author=author)
    if updated is None:
        return json.dumps({"error": f"Entity id={entity_id} not found"})
    return json.dumps(updated, indent=2, default=str)


def entity_add_reference(
    *,
    entity_id: int,
    source_path: str,
    source_kind: str,
    snippet: str | None = None,
) -> str:
    """Explicitly append a reference row for an entity.

    The standard recording path is the side effect of ``entity_resolve``
    + ``entity_create`` + ``entity_update``. This capability exists for
    scripts, tests, and dashboard-driven recording that don't ride one
    of those flows. De-dup window applies (same store default, 3600s
    per ``(entity_id, source_path, source_kind)``).
    """
    import json
    from work_buddy.entities import store as entity_store

    try:
        rid = entity_store.record_reference(
            entity_id=entity_id,
            source_path=source_path,
            source_kind=source_kind,
            snippet=snippet,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if rid is None:
        return json.dumps({"error": f"Entity id={entity_id} not found"})
    return json.dumps({
        "reference_id": rid,
        "entity_id": entity_id,
        "source_path": source_path,
        "source_kind": source_kind,
    })


def entity_list_references(
    *,
    entity_id: int,
    limit: int | None = 50,
) -> str:
    """List references for an entity, newest first.

    Default limit 50 to keep dashboard responses small. Pass an
    explicit larger limit when scraping the full history.
    """
    import json
    from work_buddy.entities import store as entity_store

    refs = entity_store.list_references(entity_id, limit=limit)
    return json.dumps({
        "entity_id": entity_id,
        "references": refs,
        "count": len(refs),
    }, indent=2, default=str)
