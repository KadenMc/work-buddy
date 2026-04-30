"""Capability callables for the email/Thunderbird integration.

Registered in :mod:`work_buddy.mcp_server.registry` (see
``_email_capabilities()``). All callables are lightweight: they instantiate
the configured provider on demand, perform one HTTP round-trip, and return
JSON-serialisable dicts. No heavy imports — keeps the gateway snappy.

Surface:
  - ``email_health``         Probe-style status, returns the bridge's /health.
  - ``email_accounts``       List accounts visible through the bridge.
  - ``email_triage_run``     One BackgroundTriageProducer pass over recent mail.
  - ``email_get``            Fetch one message by stable handle.
  - ``email_display``        Open a message in the user's mail UI.

Slice 2 — verdict pass
----------------------
``email_triage_run`` honours ``triage.verdict_pass.enabled`` from config.
When True it instantiates an :class:`LLMRunner`, builds the active-tasks /
contracts / projects context block, and asks the agent for a structured
verdict per message (mapping into the existing :data:`TRIAGE_ACTIONS`).
When False, behavior is unchanged from Slice 1: items land in the pool as
raw captures (``verdict={"raw": True}``) for human review.

Email-specific verdict mapping (stricter than the journal capability's
defaults because emails skew heavily toward "low-signal newsletter":

- ``close``            — newsletters, promotional, automated notifications
                          you've already acted on, safe to drop.
- ``create_task``      — clearly action-required (a question for you,
                          a deadline, a meeting to confirm).
- ``record_into_task`` — context for an active task — only when the
                          email's subject / sender / content is
                          UNAMBIGUOUSLY about the same work. Loose
                          keyword overlap is NOT enough.
- ``leave``            — ambiguous; default when uncertain.
- ``group``            — only when the email is part of a thread already
                          in the same triage run.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from work_buddy.email.errors import EmailError, EmailMessageNotFound
from work_buddy.email.models import EmailMessageHandle
from work_buddy.email.provider import get_email_provider
from work_buddy.email.triage_adapter import (
    EMAIL_TRIAGE_ADAPTER_NAME,
    EMAIL_TRIAGE_SOURCE,
    collect_email_candidates,
)
from work_buddy.clarify.items import TriageItem

log = logging.getLogger(__name__)


# Default body-char budget when the verdict pass is enabled. The LLM needs
# enough body to discriminate "would anything break if unread?" but the
# system prompt + active-tasks context block already eats ~1.5k tokens of
# Qwen's 4096-token window, so the body has to fit in what's left.
#
# Empirical sizing: at 800 chars (~200 tokens) we still classify
# correctly on the test corpus AND reliably stay under context. Bumped
# down from 1500 after a long-body email tripped LM Studio's
# n_keep > n_ctx check post-prompt-sharpening.
#
# Users who run a model with a larger context window (or want richer
# rationales) can override include_body_chars in the call.
_DEFAULT_VERDICT_BODY_CHARS = 800


def _provider_or_error() -> tuple[Any, dict | None]:
    try:
        return get_email_provider(), None
    except EmailError as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------


def email_health() -> dict:
    """Liveness probe — return the configured provider's health payload."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        return {"ok": True, "provider": provider.name, **provider.health()}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def email_accounts() -> dict:
    """List the accounts the bridge currently exposes."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        accounts = provider.list_accounts()
        allowed = [a for a in accounts if a.get("allowed", False)]
        return {
            "ok": True,
            "provider": provider.name,
            "accounts": accounts,
            "allowed_count": len(allowed),
        }
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def email_triage_run(
    *,
    days_back: int = 2,
    max_messages: int = 50,
    unread_only: bool = True,
    folder_path: str | None = None,
    account_id: str | None = None,
    include_body_chars: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    tier: str | None = None,
) -> dict:
    """Collect recent email candidates and run one BackgroundTriageProducer pass.

    Verdict-pass behavior is gated by ``triage.verdict_pass.enabled`` in
    config:

    - When ``False`` (Slice 1 default): items land in the pool as raw
      captures (``verdict={"raw": True}``) for human review.
    - When ``True`` (Slice 2): an :class:`LLMRunner`-backed agent
      classifies each message into one of the existing
      :data:`TRIAGE_ACTIONS` (close / create_task / record_into_task /
      leave / group) and the structured verdict is written to the pool
      via :func:`triage_submit`.

    Args:
        days_back: How far back to scan for unread mail.
        max_messages: Cap on candidates per run after dedup.
        unread_only: Skip already-read messages (default True).
        folder_path: Limit to a specific folder URI.
        account_id: Limit to a specific account.
        include_body_chars: Body-char budget per message. ``None`` (default)
            auto-picks: 0 when verdict pass is off (headers-only is plenty
            for raw capture), :data:`_DEFAULT_VERDICT_BODY_CHARS` when on
            (the LLM needs body content to discriminate newsletter vs
            action-required).
        force: Ignore the unchanged-content idempotence gate.
        dry_run: Collect candidates but don't write to the pool.
        tier: Override starting :class:`ModelTier` for the verdict agent
            (e.g. ``"local_fast"``, ``"frontier_balanced"``,
            ``"frontier_best"``). ``None`` defaults to FRONTIER_BALANCED,
            matching the journal capability.
    """
    from work_buddy.clarify.background import BackgroundTriageProducer
    from work_buddy.clarify.config import is_verdict_pass_enabled_for, load_triage_config

    provider, err = _provider_or_error()
    if err:
        return err

    cfg = load_triage_config()
    # Per-source override via ``triage.verdict_pass.sources.email.enabled``
    # wins over the global default. See
    # :func:`work_buddy.clarify.config.is_verdict_pass_enabled_for`.
    verdict_pass_enabled = is_verdict_pass_enabled_for(cfg, "email")

    # Auto-pick body budget if caller didn't override:
    #   - verdict pass off: 0 (headers-only — what Slice 1 shipped with)
    #   - verdict pass on:  _DEFAULT_VERDICT_BODY_CHARS (LLM needs content)
    effective_body_chars = (
        include_body_chars if include_body_chars is not None
        else (_DEFAULT_VERDICT_BODY_CHARS if verdict_pass_enabled else 0)
    )

    def _collect():
        return collect_email_candidates(
            provider=provider,
            days_back=days_back,
            max_messages=max_messages,
            unread_only=unread_only,
            folder_path=folder_path,
            account_id=account_id,
            include_body_chars=effective_body_chars,
        )

    if dry_run:
        items, ch = _collect()
        return {
            "status": "dry_run",
            "item_count": len(items),
            "content_hash": ch,
            "items": [it.to_dict() for it in items],
            "verdict_pass_enabled": verdict_pass_enabled,
        }

    if verdict_pass_enabled:
        agent_callable = _build_verdict_agent(cfg=cfg, tier_override=tier)
    else:
        # Producer never invokes when verdict_pass_enabled=False, but the
        # constructor demands a callable. Surface a clear error if anyone
        # ever tries to invoke it directly.
        def agent_callable(item, run_id):
            return {
                "content": "",
                "error": "verdict_pass disabled — agent must not be invoked",
                "error_kind": "verdict_pass_disabled",
            }

    producer = BackgroundTriageProducer(
        adapter_name=EMAIL_TRIAGE_ADAPTER_NAME,
        source=EMAIL_TRIAGE_SOURCE,
        collect=_collect,
        agent=agent_callable,
        enrich=False,
        verdict_pass_enabled=verdict_pass_enabled,
    )
    result = producer.run(force=force).to_dict()
    result["verdict_pass_enabled"] = verdict_pass_enabled
    return result


# ---------------------------------------------------------------------------
# Slice 2 — verdict-pass agent
# ---------------------------------------------------------------------------


_AGENT_SYSTEM_PROMPT = """\
You are triaging one email from the user's inbox. Decide the single best
next action and fill in the verdict schema.

## The core question

Before considering each action, ask: **if the user never opened this
email, would anything in their work or life materially break?**

  - **No** → it's a candidate for ``close``. The cost of a wrong
    ``close`` is low (the email still exists in Thunderbird; the user
    can re-find it). The cost of surfacing low-signal mail as a task is
    high (mental tax, trust erosion in the triage system).
  - **Yes** → it's a candidate for ``create_task`` or ``record_into_task``,
    depending on whether there's a matching active task.
  - **Genuinely cannot tell** → ``leave``.

The bar for ``close`` is therefore much lower than the bar for
``create_task``. RSVPs, deadlines, and registration links by themselves
do NOT lift an email out of ``close``: those are sender-side affordances,
not user-side obligations. Optional community events, social gatherings,
and mailing-list invitations get ``close`` even when they "could" be
attended.

## Actions

  - close              — Things the user can safely never read:
                          • mailing-list invitations to community events,
                            socials, parties, optional workshops, even
                            when they have RSVP deadlines (an invitation
                            you may decline silently is not an obligation)
                          • newsletters, digests, group announcements
                          • automated notifications ("your X is expiring",
                            "you have N unread", security info, billing
                            receipts) where there's no immediate
                            work-impacting consequence
                          • CC'd / BCC'd threads where the user is not
                            the primary actor and the discussion doesn't
                            block their work
                          • promotional / marketing email
                          When unsure between close and leave, prefer
                          close.

  - create_task        — Choose ONLY when the email contains a direct
                          ask tied to the user's obligations or active
                          work. Examples:
                          • a manager / collaborator asks the user a
                            substantive question that needs an answer
                          • a deadline that affects something the user
                            has committed to (a paper, a project, a
                            meeting they said they'd attend)
                          • a process step the user must complete
                            (form, document review, approval) tied to
                            a real obligation
                          • a personal request from someone who knows
                            the user and is asking them specifically
                          Mailing-list invitations are NOT create_task
                          by default — choose close unless the user's
                          context block shows a related contract or
                          project that makes attendance work-relevant.
                          Include ``suggested_task_text`` (≤80 chars):
                          name the ACTION TO DO ("Reply to Ali about
                          May 5 ECG meeting"), not the email
                          ("Ali sent ECG meeting email").

  - record_into_task   — The email is UNAMBIGUOUSLY about an active
                          task already in the user's context block.
                          Same system, same project, same subject.
                          Include ``target_task_id`` copied VERBATIM
                          from the Active Tasks list. Loose keyword
                          overlap is NOT enough; quote the matching
                          task title in the rationale. NEVER invent a
                          task_id. If you considered create_task and
                          there's a matching active task, prefer
                          record_into_task.

  - leave              — RESERVED for "I genuinely cannot determine
                          the intent" — e.g., a one-line cryptic
                          message from an unknown sender, or text
                          that's mostly an image you can't read. The
                          bar for leave is HIGH; close is the stronger
                          default for low-signal mail. Don't pick
                          leave just because the email is vaguely
                          interesting.

  - group              — RARE for emails. Only when this message
                          obviously clusters with another item already
                          in the SAME triage run (e.g., the third reply
                          in an active thread that's also being triaged).
                          Include ``related_item_ids``. If the related
                          items aren't in the run, do NOT use group;
                          choose another action.

## Context

The user message includes a ``## User's Current Context`` block with
active tasks, contracts, and projects. READ IT BEFORE DECIDING.

- If the email references an Active Contract or Project, say so in the
  rationale and prefer ``record_into_task`` if there's a matching task.
  If there's a clearly-related contract/project but no specific task,
  ``create_task`` with a suggested_task_text mentioning the contract
  is appropriate.
- If the email is from a mailing list AND the user's context shows no
  related work, prefer ``close`` regardless of any RSVP / deadline
  language in the email.
- An empty or thin context block is NOT a reason to create tasks
  defensively — it just means the user has few active commitments
  right now. Emails still need to clear the "would anything break?"
  bar.

## group_intent (required)

A short noun-phrase (3–8 words) naming the UNDERLYING INTENT — NOT
the action name, NOT a restatement of the subject line. Shown as the
card title in the Review UI; it should help the user recognize what
the email is about at a glance.

Good:
  subject: "RE: Discussion on UHN ECG Data Extraction Experience"
    → group_intent: "UHN ECG data extraction follow-up"
  subject: "You are invited to join Vector Community Day on May 14, 2026!"
    → group_intent: "Vector Community Day invitation"

Bad:
  - "Create task"             (that's the action name)
  - "Email asks about X"      (that's the rationale)
  - the subject line verbatim
  - empty / one-word

## Rationale

One to three sentences. Cite specific email content (sender role,
subject phrasing, concrete asks or absence of any) so the reviewer
can verify your reasoning. For ``close``, briefly say why nothing
breaks if the user never reads it.
"""


def _build_verdict_agent(
    *,
    cfg: dict,
    tier_override: str | None,
):
    """Construct the per-item agent callable.

    Builds the active-tasks / contracts / projects context block once
    per run, instantiates a single :class:`LLMRunner`, and returns a
    closure that the producer will invoke per :class:`TriageItem`.

    Mirrors the structure of
    :func:`work_buddy.clarify.capabilities.journal_triage_scan.journal_triage_scan`.
    """
    from work_buddy.llm import LLMRunner, ModelTier

    # Resolve tier
    if tier_override:
        try:
            tier = ModelTier(tier_override)
        except ValueError as exc:
            raise ValueError(
                f"Unknown tier {tier_override!r}. "
                f"Valid: {[t.value for t in ModelTier]}"
            ) from exc
    else:
        tier = ModelTier.FRONTIER_BALANCED

    # Build active-tasks context block once per run
    triage_context_block = _build_context_block(cfg)
    runner = LLMRunner()

    def _agent(item: TriageItem, run_id: str) -> dict[str, Any]:
        return _invoke_email_agent(
            runner=runner,
            item=item,
            run_id=run_id,
            tier=tier,
            triage_context_block=triage_context_block,
        )

    return _agent


def _build_context_block(cfg: dict) -> str:
    """Render the user's current-context block for injection into the
    per-item prompt. Best-effort: failures yield an empty string."""
    ctx_cfg = cfg.get("triage_context", {}) or {}
    try:
        from work_buddy.clarify.recommend import (
            build_triage_context, render_triage_context_block,
        )
        triage_context = build_triage_context(
            task_states=ctx_cfg.get(
                "task_states", ["focused", "mit", "inbox"],
            ),
            max_tasks=ctx_cfg.get("max_tasks", 12),
        )
        # Drop recent_commits — not useful for email classification, eats
        # tokens. Same call the journal capability makes.
        if not ctx_cfg.get("include_recent_commits", False):
            triage_context.pop("recent_commits", None)
        return render_triage_context_block(triage_context)
    except Exception as exc:
        log.warning("email_triage: build_triage_context failed: %s", exc)
        return ""


def _invoke_email_agent(
    *,
    runner: Any,                 # LLMRunner; not type-hinted to keep imports light
    item: TriageItem,
    run_id: str,
    tier: Any,                   # ModelTier
    triage_context_block: str = "",
) -> dict[str, Any]:
    """Call the unified runner with a constrained verdict schema.

    On success, parses the structured output, calls :func:`triage_submit`
    to write the pool entry, and returns a dict shaped the way
    :class:`BackgroundTriageProducer` expects (``content`` / ``error`` /
    ``error_kind``) so its submission-check path works unchanged.
    """
    from work_buddy.llm import ErrorKind
    from work_buddy.clarify.capabilities.triage_submit import triage_submit
    from work_buddy.clarify.verdict_call import call_for_verdict
    from work_buddy.clarify.verdict_schema import VERDICT_SCHEMA, verdict_to_submit_kwargs

    user_prompt = _render_email_prompt(
        item=item, run_id=run_id, triage_context_block=triage_context_block,
    )

    resp = call_for_verdict(
        runner=runner,
        tier=tier,
        system=_AGENT_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=VERDICT_SCHEMA,
        caller="email_triage",
        item_id=item.id,
    )

    if resp.is_error():
        log.warning(
            "email_triage: LLM failed for item %s on tier %s (%s): %s",
            item.id, resp.tier_used, resp.error_kind, resp.error,
        )
        return {
            "content": resp.content,
            "error": resp.error,
            "error_kind": resp.error_kind.value if resp.error_kind else None,
        }

    verdict = resp.structured_output or {}
    submit_kwargs = verdict_to_submit_kwargs(verdict)
    submit_result = triage_submit(
        run_id=run_id,
        item_id=item.id,
        **submit_kwargs,
    )

    if submit_result.get("status") != "ok":
        log.warning(
            "email_triage: triage_submit rejected verdict for item %s: %s",
            item.id, submit_result,
        )
        return {
            "content": resp.content,
            "error": f"triage_submit rejected: {submit_result.get('error', 'unknown')}",
            "error_kind": ErrorKind.BAD_REQUEST.value,
        }

    return {
        "content": resp.content or "",
        "verdict": verdict,
        "tier_used": resp.tier_used,
    }


def _render_email_prompt(
    *,
    item: TriageItem,
    run_id: str,
    triage_context_block: str = "",
) -> str:
    """Compose the per-item user prompt for the email verdict agent.

    Layout:
      1. Item id + run id (so the agent doesn't need to copy them anywhere
         — they're populated by the dispatch path, but visible for trace).
      2. Email-specific metadata (sender, recipients, date, folder).
      3. Global "User's Current Context" block.
      4. Email content (subject + from + body if present).
      5. Closing instruction.
    """
    meta = item.metadata or {}
    sender = meta.get("sender") or "(unknown sender)"
    recipients = meta.get("recipients") or ""
    date = meta.get("date") or ""
    folder = meta.get("folder") or ""
    folder_type = meta.get("folder_type") or ""

    meta_lines = [
        f"From: {sender}",
    ]
    if recipients:
        meta_lines.append(f"To: {recipients}")
    if date:
        meta_lines.append(f"Date: {date}")
    if folder or folder_type:
        ft = f"{folder} ({folder_type})" if folder_type else folder
        meta_lines.append(f"Folder: {ft}")
    meta_block = "\n".join(meta_lines)

    ctx_block = f"\n{triage_context_block}\n" if triage_context_block else ""

    return (
        f"Item id: {item.id}\n"
        f"\n--- Email metadata ---\n"
        f"{meta_block}\n"
        f"--- End metadata ---\n"
        f"{ctx_block}"
        f"\n--- Email content ---\n"
        f"{item.text.strip()}\n"
        f"--- End email ---\n"
        f"\nReturn ONLY the JSON verdict object. No prose, no markdown fences."
    )


# ---------------------------------------------------------------------------
# Single-message follow-ups
# ---------------------------------------------------------------------------


def email_get(
    *,
    provider_message_id: str,
    folder_path: str,
    max_body_chars: int = 8000,
) -> dict:
    """Fetch one message including body. Operates on the operational handle
    (provider_message_id + folder_path), not the stable key."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not provider_message_id or not folder_path:
        return {"ok": False, "error": "provider_message_id and folder_path are required",
                "error_kind": "bad_request"}
    handle = EmailMessageHandle(
        provider_message_id=provider_message_id, folder_path=folder_path,
    )
    try:
        msg = provider.get_message(handle, max_body_chars=max_body_chars)
        return {"ok": True, "provider": provider.name, **msg.to_dict()}
    except EmailMessageNotFound as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def email_display(
    *,
    provider_message_id: str,
    folder_path: str,
    mode: str = "3pane",
) -> dict:
    """Open a message in Thunderbird's UI. ``mode`` is one of
    ``3pane`` (focus the message in the main folder pane), ``tab``, or
    ``window``."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not provider_message_id or not folder_path:
        return {"ok": False, "error": "provider_message_id and folder_path are required",
                "error_kind": "bad_request"}
    handle = EmailMessageHandle(
        provider_message_id=provider_message_id, folder_path=folder_path,
    )
    try:
        return {"ok": True, "provider": provider.name,
                **provider.display_message(handle, mode=mode)}
    except EmailMessageNotFound as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
