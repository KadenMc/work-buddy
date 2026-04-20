"""``inline_triage_scan`` capability — user-initiated single-selection triage.

Thin adapter around :class:`BackgroundTriageProducer`, mirroring
``journal_triage_scan`` but driven by an Obsidian right-click handoff
rather than a cron cadence.

The user selects text (or their cursor paragraph), optionally types a
hint, and the ``send-to-agent`` inline handler kicks off this capability
in a background thread. The capability builds one TriageItem via
:func:`work_buddy.triage.adapters.inline.collect_inline_selection`,
enriches it with IR context, and hands it to the local-LLM
``triage_agent`` preset to produce a verdict in the pending-review pool.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.background import BackgroundTriageProducer
from work_buddy.triage.items import TriageItem

logger = get_logger(__name__)


_AGENT_SYSTEM_PROMPT = """\
You are triaging one selection a user sent from Obsidian.

Call `wb_run` exactly once with:
  capability: "triage_submit"
  params: {
    run_id: <from user message>,
    item_id: <from user message>,
    recommended_action: "create_task" | "record_into_task" | "leave" | "close" | "group",
    rationale: one to three sentences,
    group_intent: short noun-phrase naming the intent (≤8 words),
  }

Action guide:
  - create_task: new actionable work (add suggested_task_text)
  - record_into_task: add detail to an existing task (add target_task_id)
  - leave: keep in notes as-is
  - close: safe to drop
  - group: belongs with sibling items

Weight the user's hint heavily. If uncertain, pick "leave" — never skip submission.
"""


def inline_triage_scan(
    *,
    file_path: str,
    selection: str = "",
    paragraph: str = "",
    cursor_line: int = 0,
    hint: str = "",
    force: bool = True,
    profile: str | None = None,
    enrich: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single triage pass over one user-sent selection.

    Args:
        file_path: Vault-relative source path (for provenance).
        selection: The user's literal selection; falls back to paragraph.
        paragraph: Surrounding paragraph (used when selection is empty).
        cursor_line: 0-indexed cursor line in the source file.
        hint: Optional user-typed intent hint from the modal.
        force: Default ``True`` — user-initiated clicks should re-run
            even if the same selection was sent before.
        profile: Override the configured ``triage.agent_profile``.
        enrich: Pre-fetch hybrid-IR context for the selection.
        dry_run: Collect + enrich, skip the agent loop.

    Returns:
        Status dict (see :class:`ProducerResult.to_dict`).
    """
    from work_buddy.triage.config import load_triage_config, resolve_profile

    cfg = load_triage_config()
    agent_profile = resolve_profile(cfg, "agent", override=profile)
    agent_cfg = cfg.get("agent", {}) or {}

    def _collect() -> tuple[list[TriageItem], str | None]:
        from work_buddy.triage.adapters.inline import collect_inline_selection
        return collect_inline_selection(
            file_path=file_path,
            selection=selection,
            paragraph=paragraph,
            cursor_line=cursor_line,
            hint=hint,
        )

    if dry_run:
        items, ch = _collect()
        return {
            "status": "dry_run",
            "item_count": len(items),
            "content_hash": ch,
            "items": [it.to_dict() for it in items],
        }

    # Build the user-context packet once per pass. This is the same
    # `build_triage_context()` the Chrome intent-group pass uses —
    # active tasks / contracts / projects / recent commits — which
    # reliably outperforms IR enrichment on short inline selections
    # (where IR tends to surface coincidental-vocabulary noise).
    #
    # Chrome can pass the full packet to Sonnet (200K context). Here
    # the packet is rendered into a local-LLM prompt with an 8K limit,
    # so we truncate per-section before handing to ``_render_item_prompt``.
    from work_buddy.triage.recommend import build_triage_context
    raw_context = build_triage_context() if enrich else {}
    triage_context = _truncate_context_for_local(raw_context)

    def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        return _invoke_agent(
            item=item,
            run_id=run_id,
            context=triage_context,
            profile=agent_profile,
            max_tokens=agent_cfg.get("max_tokens", 1024),
            temperature=agent_cfg.get("temperature", 0.0),
        )

    # ``enrich=False`` on the producer disables IR enrichment — inline
    # uses the ``build_triage_context`` packet above instead, so per-item
    # IR hits would just add noise and prompt length.
    producer = BackgroundTriageProducer(
        adapter_name="inline_triage",
        source="inline",
        collect=_collect,
        agent=_agent,
        enrich=False,
    )
    return producer.run(force=force).to_dict()


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _truncate_context_for_local(
    context: dict[str, Any],
    *,
    max_tasks: int = 12,
    max_projects: int = 6,
    max_commits: int = 8,
    max_task_text_chars: int = 140,
    max_project_desc_chars: int = 140,
) -> dict[str, Any]:
    """Cap each section of ``build_triage_context`` so the rendered block
    stays small enough for a local model's context window.

    Tasks are already one-liners from the master task list (truncated
    here only for pathological cases). Projects come with full
    descriptions (1–2KB each!) — those MUST be cut to one sentence or
    they alone blow the budget. Commits are already one-liners from
    ``git log --oneline``. All contracts are kept (rare, high-signal).

    Tasks are ordered by state priority — focused → mit → inbox — so the
    most-relevant items survive truncation.

    NOTE: The agent only sees the task LINE (description), not the full
    task note. If we ever expose ``task_get`` to the inline preset, the
    agent could fetch the note on demand for items that look relevant.
    For now, line-only seems to be enough signal at the active-task
    counts we observe (~30 max).
    """
    if not context:
        return {}

    state_rank = {"focused": 0, "mit": 1, "inbox": 2}
    tasks = sorted(
        context.get("active_tasks", []) or [],
        key=lambda t: state_rank.get(t.get("state", ""), 99),
    )
    truncated_tasks = []
    for t in tasks[:max_tasks]:
        text = t.get("text", "")
        if len(text) > max_task_text_chars:
            text = text[: max_task_text_chars - 1].rstrip() + "…"
        truncated_tasks.append({**t, "text": text})

    truncated_projects = []
    for p in (context.get("active_projects", []) or [])[:max_projects]:
        desc = p.get("description", "") or ""
        if len(desc) > max_project_desc_chars:
            # Prefer to cut on the first sentence boundary if there's
            # one in range — gives a cleaner one-liner than mid-word.
            first_sentence = desc.split(". ", 1)[0]
            if first_sentence and len(first_sentence) <= max_project_desc_chars:
                desc = first_sentence + ("." if not first_sentence.endswith(".") else "")
            else:
                desc = desc[: max_project_desc_chars - 1].rstrip() + "…"
        truncated_projects.append({**p, "description": desc})

    return {
        "active_tasks": truncated_tasks,
        "active_contracts": context.get("active_contracts", []) or [],
        "active_projects": truncated_projects,
        "recent_commits": (context.get("recent_commits", []) or [])[:max_commits],
    }


def _invoke_agent(
    *,
    item: TriageItem,
    run_id: str,
    context: dict[str, Any],
    profile: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Call ``llm_with_tools`` with the triage_submit-only preset."""
    from work_buddy.llm.with_tools import llm_with_tools

    user_prompt = _render_item_prompt(
        item=item, run_id=run_id, context=context,
    )
    return llm_with_tools(
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        profile=profile,
        # Narrow preset keeps the wb_run tool schema small enough to
        # fit in local-model context windows (triage_agent 500s here).
        tool_preset="triage_submit_only",
        required_capabilities=["triage_submit"],
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _render_item_prompt(
    *,
    item: TriageItem,
    run_id: str,
    context: dict[str, Any],
) -> str:
    """Compose the per-item user prompt with file + hint + user context.

    ``context`` is the output of
    :func:`work_buddy.triage.recommend.build_triage_context` — active
    tasks, contracts, projects, and recent commits.
    """
    from work_buddy.triage.recommend import render_triage_context_block

    meta = item.metadata or {}
    file_path = meta.get("file_path", "") or "(unknown)"
    cursor_line = meta.get("cursor_line", 0)
    hint = meta.get("hint", "") or "(none)"

    context_block = render_triage_context_block(context) if context else ""
    context_block = f"\n\n{context_block}\n" if context_block else ""

    return (
        f"Triage run id: {run_id}\n"
        f"Item id: {item.id}\n"
        f"File: {file_path}:{cursor_line}\n"
        f"Hint: {hint}\n"
        f"\n--- Selection ---\n"
        f"{item.text.strip()}\n"
        f"--- End ---"
        f"{context_block}"
        f"\nDecide one action for this selection, then submit it by "
        f"calling wb_run with capability='triage_submit' and params "
        f"including run_id={run_id!r} and item_id={item.id!r}."
    )
