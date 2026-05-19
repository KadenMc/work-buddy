"""Context-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    from work_buddy.mcp_server.registry import _context_block, _context_drill_down

    from work_buddy.mcp_server.context_wrappers import (
        get_git_context,
        get_obsidian_context,
        get_tasks_context,
        get_wellness_context,
        get_chat_context,
        get_chrome_context,
        get_messages_context,
        get_smart_context,
        get_calendar_context,
        get_projects_context,
        collect_bundle,
        chrome_activity,
        chrome_infer,
        chrome_content,
        chrome_tab_close,
        chrome_tab_group,
        chrome_tab_move,
        llm_costs,
        datacore_status,
        datacore_query,
        datacore_fullquery,
        datacore_validate,
        datacore_get_page,
        datacore_evaluate,
        datacore_schema,
        datacore_compile_plan,
        datacore_run_plan,
        vault_recon,
    )
    from work_buddy.collectors.vault_recon_collector import (
        vault_recon_collect,
    )
    from work_buddy.embedding.client import ir_index as _ir_index_client
    from work_buddy.sessions.inspector import (
        session_get as _session_get,
        session_expand as _session_expand,
        session_locate as _session_locate,
        session_search as _session_search,
        session_commits as _session_commits,
        session_uncommitted as _session_uncommitted,
        session_wb_activity as _session_wb_activity,
    )

    def _format_result_header(r: dict) -> str:
        """Render the per-result header line, dispatching on source.

        Each source surfaces different identifying metadata; the conversation
        format ``[project] session`` is meaningless for a task note hit.
        Unknown sources fall back to ``[source] doc_id`` so a header always
        renders something useful even before a dedicated formatter exists.
        """
        meta = r.get("metadata", {}) or {}
        source = r.get("source", "")
        doc_id = r.get("doc_id", "")

        if source == "conversation":
            proj = meta.get("project_name") or "?"
            sid = (meta.get("session_id") or "?")[:12]
            return f"### [{proj}] {sid}"

        if source == "task_note":
            tid = meta.get("task_id") or "?"
            state = meta.get("task_state") or "?"
            return f"### [task] {tid} ({state})"

        if source == "docs":
            kind = meta.get("kind") or "doc"
            path = meta.get("path") or doc_id
            return f"### [{kind}] {path}"

        if source == "chrome":
            title = meta.get("title") or meta.get("tab_title") or "?"
            return f"### [tab] {title[:80]}"

        if source == "projects":
            slug = meta.get("slug") or doc_id
            name = meta.get("name") or slug
            return f"### [project] {name} ({slug})"

        # Generic fallback — no source-specific knowledge needed.
        return f"### [{source or 'result'}] {doc_id[:48]}"

    def _format_results(results: list[dict], label: str) -> str:
        """Format structured result dicts into markdown."""
        if not results:
            return f"No results from {label}."

        lines = [f"*{len(results)} result(s) from {label}*", ""]
        for r in results:
            lines.append(_format_result_header(r))
            scores = []
            if r.get("bm25_score"):
                scores.append(f"bm25={r['bm25_score']:.3f}")
            if r.get("dense_score"):
                scores.append(f"dense={r['dense_score']:.3f}")
            # Per-projection breakdown for multi-projection sources (e.g.
            # task_note's body / line). Only surfaces when present so the
            # conversation/docs cases stay terse.
            proj_scores = r.get("projection_scores") or {}
            for key in sorted(proj_scores):
                val = proj_scores[key]
                if val:
                    scores.append(f"{key}={val:.3f}")
            if r.get("recency_weight") is not None:
                scores.append(f"recency={r['recency_weight']:.2f}")
            if scores:
                lines.append(f"*Score: {r['score']:.4f} ({', '.join(scores)})*")
            else:
                lines.append(f"*Score: {r['score']:.4f}*")
            lines.append("")
            if r.get("display_text"):
                preview = r["display_text"][:300]
                lines.append(f"> {preview}")
                lines.append("")
        return "\n".join(lines)

    def _ir_search_dispatch(
        query: str,
        *,
        top_k: int = 10,
        source: str | None = None,
        scope: str | None = None,
        method: str = "keyword,semantic",
        recency: bool | None = None,
    ) -> str:
        """Search indexed content using configurable method(s).

        Thin wrapper: delegates to ir.search.search() for structured results,
        then formats to markdown via _format_results().
        """
        from work_buddy.ir.search import search as _ir_search

        results = _ir_search(
            query, top_k=top_k, source=source, scope=scope,
            method=method, recency=recency,
        )
        if isinstance(results, str):
            return results  # error message

        methods = [m.strip() for m in method.split(",") if m.strip()]
        label = "+".join(methods)
        if len(methods) > 1:
            label += " (RRF fused)"
        return _format_results(results, label)

    def _ir_index_dispatch(
        action: str = "build",
        source: str = "conversation",
        days: int = 30,
        force: bool = False,
    ) -> str:
        """Build or check the IR index via the embedding service."""
        import json

        result = _ir_index_client(
            action, source=source, days=days, force=force,
        )
        if result is None:
            return json.dumps({
                "error": "Embedding service unavailable. Start it with: Start-ScheduledTask -TaskName 'WB-Embedding'"
            })
        return json.dumps(result, indent=2)

    register_op("op.wb.context_git", get_git_context)
    register_op("op.wb.context_obsidian", get_obsidian_context)
    register_op("op.wb.context_tasks", get_tasks_context)
    register_op("op.wb.context_wellness", get_wellness_context)
    register_op("op.wb.context_chat", get_chat_context)
    register_op("op.wb.context_search", _ir_search_dispatch)
    register_op("op.wb.session_get", _session_get)
    register_op("op.wb.session_expand", _session_expand)
    register_op("op.wb.session_locate", _session_locate)
    register_op("op.wb.session_search", _session_search)
    register_op("op.wb.session_commits", _session_commits)
    register_op("op.wb.session_uncommitted", _session_uncommitted)
    register_op("op.wb.session_wb_activity", _session_wb_activity)
    register_op("op.wb.ir_index", _ir_index_dispatch)
    register_op("op.wb.context_chrome", get_chrome_context)
    register_op("op.wb.chrome_activity", chrome_activity)
    register_op("op.wb.chrome_infer", chrome_infer)
    register_op("op.wb.chrome_content", chrome_content)
    register_op("op.wb.chrome_tab_close", chrome_tab_close)
    register_op("op.wb.chrome_tab_group", chrome_tab_group)
    register_op("op.wb.chrome_tab_move", chrome_tab_move)
    register_op("op.wb.chrome_route_to_tasks", lambda **kw: __import__('work_buddy.collectors.chrome_thread_actions', fromlist=['chrome_route_to_tasks']).chrome_route_to_tasks(**kw))
    register_op("op.wb.chrome_route_to_umbrella_task", lambda **kw: __import__('work_buddy.collectors.chrome_thread_actions', fromlist=['chrome_route_to_umbrella_task']).chrome_route_to_umbrella_task(**kw))
    register_op("op.wb.llm_costs", llm_costs)
    register_op("op.wb.context_messages", get_messages_context)
    register_op("op.wb.context_smart", get_smart_context)
    register_op("op.wb.context_calendar", get_calendar_context)
    register_op("op.wb.datacore_status", datacore_status)
    register_op("op.wb.datacore_query", datacore_query)
    register_op("op.wb.datacore_fullquery", datacore_fullquery)
    register_op("op.wb.datacore_validate", datacore_validate)
    register_op("op.wb.datacore_get_page", datacore_get_page)
    register_op("op.wb.datacore_evaluate", datacore_evaluate)
    register_op("op.wb.datacore_schema", datacore_schema)
    register_op("op.wb.vault_recon", vault_recon)
    register_op("op.wb.datacore_compile_plan", datacore_compile_plan)
    register_op("op.wb.datacore_run_plan", datacore_run_plan)
    register_op("op.wb.vault_recon_collect", vault_recon_collect)
    register_op("op.wb.context_projects", get_projects_context)
    register_op("op.wb.context_bundle", collect_bundle)
    register_op("op.wb.context_block", _context_block)
    register_op("op.wb.context_drill_down", _context_drill_down)


_register()
