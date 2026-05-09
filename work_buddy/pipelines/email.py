"""Email-triage source pipeline.

Wires the existing email collection + per-source action library into
the unified :class:`SourcePipeline` shape. End-to-end flow:

1. **collect** — fetch recent unread mail via the configured email
   provider (Thunderbird bridge by default) using
   :func:`work_buddy.email.triage_adapter.collect_email_candidates`.
   Returns zero or more :class:`CapturedItem` instances with source
   ``"email_message"``. Bridge unavailability degrades silently to
   an empty collection.
2. **annotate_items** — light-touch tag synthesis from sender domain,
   folder, and metadata flags (read / flagged). No LLM call: emails
   already carry rich label/preview/sender content; the
   cluster-refinement step downstream is where the LLM signal goes.
3. **precluster** — embedding-fused clustering reusing
   ``clarify/cluster.cluster_items``. Email items skip proximity
   weighting (no spatial relationship like Chrome's window/index);
   the algorithmic clusterer leans on subject + sender + tag overlap.
4. **umbrella_summary** — title pattern
   ``"Email triage: {N} unread from {M} senders"``.

Stage 4 (LLM cluster refinement) runs through the shared
:func:`work_buddy.pipelines.refine_clusters`. Tier chain comes from
``triage.refine_clusters.tier_chain`` (local-first by default).

Action library
--------------

Per-source actions: close (advisory dismiss), create one task per
email, create one umbrella task for the cluster. The universal
library (``thread_dismiss`` / ``thread_defer`` / ``thread_rename``)
layers on top.

Why not include archive / unsubscribe / reply? The companion
extension (``KadenMc/thunderbird-work-buddy``) is read-first in v1.
Mutating actions belong with a future v2 extension permission set,
behind explicit consent. ``email_close`` does the closest defensible
thing today — dismiss the Thread so it stops appearing as work.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from work_buddy.pipelines.actions import (
    CARDINALITY_PER_GROUP,
    ActionDescriptor,
    ActionLibrary,
)
from work_buddy.pipelines.types import CapturedItem, ClusterSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action library — email-specific descriptors
# ---------------------------------------------------------------------------


EMAIL_ACTIONS: list[ActionDescriptor] = [
    ActionDescriptor(
        capability_name="email_close",
        label="Close cluster",
        description=(
            "Mark this email cluster as not actionable — newsletters, "
            "automated notifications, and similar low-signal mail. "
            "Advisory only: dismisses the Thread without touching the "
            "underlying mailbox (Thunderbird bridge is read-first in v1)."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="x-square",
    ),
    ActionDescriptor(
        capability_name="email_create_tasks",
        label="Create one task per email",
        description=(
            "Walk each email in this cluster and create a task in the "
            "master task list. The subject becomes the task text; "
            "sender + date land in the linked summary note. Use when "
            "every email in the cluster is independently actionable."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="check-square",
    ),
    ActionDescriptor(
        capability_name="email_create_umbrella_task",
        label="Create umbrella task",
        description=(
            "Create a single task representing the whole cluster. "
            "The cluster label becomes the task text; the linked "
            "summary note lists every email's subject + sender + date "
            "for context. Use when the cluster is one piece of work "
            "(e.g. a thread that needs a single response, or a set of "
            "PR notifications to review together)."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="package",
    ),
    ActionDescriptor(
        capability_name="email_record_into_task",
        label="Record into existing task",
        description=(
            "File the cluster's emails as a context section on an "
            "existing task's linked note. Use when the cluster is "
            "*context for ongoing work* — replies on an active "
            "deliverable, PR-review notifications about a task you're "
            "already tracking — rather than a new task itself. The "
            "user picks the target task at approval time; the action "
            "appends a bulleted Emails-recorded section listing each "
            "email's subject + sender + date."
        ),
        cardinality=CARDINALITY_PER_GROUP,
        icon="file-plus",
    ),
]


EMAIL_ACTION_LIBRARY = ActionLibrary(EMAIL_ACTIONS)
"""The Email pipeline's action library. Merged with universal actions
by the runner."""


# ---------------------------------------------------------------------------
# Item conversion + tag synthesis
# ---------------------------------------------------------------------------


# Whitelist of metadata fields kept on the CapturedItem.payload. The
# email triage adapter's TriageItem.metadata is rich; pruning keeps
# the CapturedItem payload small and predictable.
_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "stable_key",
    "rfc_message_id",
    "provider_message_id",
    "folder_path",
    "folder",
    "folder_type",
    "account_id",
    "sender",
    "recipients",
    "cc",
    "subject",
    "date",
    "tags",
    "read",
    "flagged",
})


def _captured_from_triage_item(ti: Any) -> CapturedItem:
    """Convert an email :class:`TriageItem` into a
    :class:`CapturedItem`. Carries the operational handle + RFC
    message-id in ``payload`` so per-cluster actions can re-fetch the
    body or open the message in Thunderbird later.
    """
    md = ti.metadata or {}
    payload = {k: md[k] for k in _PAYLOAD_FIELDS if k in md}
    label = ti.label or md.get("subject") or ti.id or "(email)"
    # Truncate label so dashboard cards stay scannable.
    if len(label) > 80:
        label = label[:79] + "…"
    return CapturedItem(
        id=ti.id,
        source="email_message",
        type="email",
        label=label,
        payload=payload,
        # Use the adapter's already-composed text (subject + from +
        # preview + body if any) as the summary so refine_clusters has
        # rich content to work with.
        summary=ti.text or None,
    )


def _domain_of(sender: str) -> str:
    """Extract a lowercase domain from a sender address.

    Handles the common ``Display Name <user@host>`` form as well as
    bare ``user@host``. Returns ``""`` on parse failure rather than
    raising — tag synthesis is best-effort.
    """
    if not sender:
        return ""
    # Take what's between < and > if present, else the raw string.
    if "<" in sender and ">" in sender:
        try:
            sender = sender.split("<", 1)[1].split(">", 1)[0]
        except Exception:
            pass
    if "@" not in sender:
        return ""
    domain = sender.rsplit("@", 1)[-1].strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _singleton_label(ci: CapturedItem) -> str:
    """Per-email cluster label — uses the subject (truncated) so the
    dashboard's per-cluster card title reads like an email subject
    line rather than "Cluster of 1".
    """
    payload = ci.payload or {}
    subject = (payload.get("subject") or ci.label or "(no subject)").strip()
    if len(subject) > 80:
        subject = subject[:79] + "…"
    return subject or "(no subject)"


def _synthesised_tags(payload: dict[str, Any]) -> tuple[str, ...]:
    """Build a small set of tags from the email's metadata + flags.

    These feed the algorithmic clusterer's tag-Jaccard signal so
    senders / folders / flag-states cluster naturally even when the
    embedding service is unavailable.
    """
    tags: list[str] = []

    domain = _domain_of(payload.get("sender") or "")
    if domain:
        tags.append(f"sender:{domain}")

    folder_type = (payload.get("folder_type") or "").lower()
    if folder_type:
        tags.append(f"folder:{folder_type}")

    if payload.get("flagged"):
        tags.append("flagged")
    if not payload.get("read", True):
        tags.append("unread")

    # Carry through any source-side tags the email itself declares
    # (Thunderbird message tags, Gmail labels). Prefix to disambiguate
    # from the synthesised ones above.
    raw_tags = payload.get("tags") or []
    for t in raw_tags:
        s = str(t).strip()
        if not s:
            continue
        tags.append(f"label:{s}")

    return tuple(tags)


# ---------------------------------------------------------------------------
# Pipeline implementation
# ---------------------------------------------------------------------------


class EmailTriagePipeline:
    """The email-triage data source.

    Implements the :class:`work_buddy.pipelines.SourcePipeline`
    protocol. Construct without arguments; per-run configuration
    (``days_back``, ``max_messages``, ``unread_only``,
    ``folder_path``, ``account_id``, ``include_body_chars``) flows in
    via ``run_pipeline`` kwargs and lands as ``collect_kwargs`` in
    :meth:`collect`.
    """

    name = "email_triage"

    @property
    def action_library(self) -> ActionLibrary:
        return EMAIL_ACTION_LIBRARY

    # ------------------------------------------------------------------
    # Stage 1 — collect
    # ------------------------------------------------------------------

    def collect(
        self,
        *,
        days_back: int = 2,
        max_messages: int = 50,
        unread_only: bool = True,
        folder_path: str | None = None,
        account_id: str | None = None,
        include_body_chars: int | None = None,
        **_unused: Any,
    ) -> list[CapturedItem]:
        """Fetch recent email candidates via the configured provider.

        Wraps :func:`work_buddy.email.triage_adapter.collect_email_candidates`.
        On bridge unavailability the helper logs and returns
        ``([], None)``; we mirror that by returning an empty list so
        the runner spawns an empty umbrella the user can see.

        ``include_body_chars`` defaults to ``0`` (headers only) when
        unspecified. Earlier versions of the email triage path bumped
        this to ``800`` chars when the verdict-pass was enabled, but
        the new pipeline does its LLM work at the cluster level (in
        :func:`refine_clusters`) where labels/previews are sufficient
        for cluster-boundary decisions. Callers who want the LLM to
        see message bodies for finer-grained refinement can override.
        """
        from work_buddy.email.triage_adapter import collect_email_candidates

        body_chars = include_body_chars if include_body_chars is not None else 0
        triage_items, _content_hash = collect_email_candidates(
            days_back=days_back,
            max_messages=max_messages,
            unread_only=unread_only,
            folder_path=folder_path,
            account_id=account_id,
            include_body_chars=body_chars,
        )
        return [_captured_from_triage_item(ti) for ti in triage_items]

    # ------------------------------------------------------------------
    # Stage 2 — annotate (synthesise tags from metadata; no LLM)
    # ------------------------------------------------------------------

    def annotate_items(
        self, items: list[CapturedItem],
    ) -> list[CapturedItem]:
        """Attach tags synthesised from email metadata.

        No LLM call here — emails already carry subject + sender +
        preview that label/summary the item cleanly. Synthesised tags
        feed the algorithmic clusterer's tag-Jaccard signal so the
        clusterer behaves sensibly even when the embedding service is
        down.
        """
        if not items:
            return items
        return [
            ci.augment(tags=_synthesised_tags(ci.payload))
            for ci in items
        ]

    # ------------------------------------------------------------------
    # Stage 3 — precluster (one-cluster-per-email by design)
    # ------------------------------------------------------------------

    def precluster(
        self, items: list[CapturedItem],
    ) -> list[ClusterSpec]:
        """Return one singleton cluster per email — no algorithmic
        grouping.

        Email is **per-item** triage: each message gets its own
        action proposal ("close this newsletter", "create a task from
        this customer reply", "record this CI notification onto
        t-ab12cd34"). Algorithmic clustering — which works for Chrome
        tabs (8 tabs about LSTM training) and journal segments (5
        paragraphs about figure 3) — would mostly invent noise on
        unrelated inbox messages, forcing the user to mentally
        un-group what the agent grouped.

        Conversation-thread grouping (multiple messages of the same
        RFC reply chain) is a real signal but Thunderbird already
        groups by thread natively at its UI layer, and the bridge's
        dedup-by-stable_key collapses Gmail labels-as-folders
        duplicates within a single run. Re-grouping on top of that
        adds little. If we ever want agent-driven thread grouping,
        it would be a separate pipeline stage that consumes
        In-Reply-To / References headers — not the same code path
        as the chrome/journal Louvain clusterer.

        Each returned cluster carries:
        - ``label`` — the email subject (truncated). The dashboard
          renders this as the per-card title.
        - ``item_ids`` — exactly one id (the email's CapturedItem.id).

        On empty input → empty output (the runner spawns an empty
        umbrella so the operator sees the run executed).
        """
        if not items:
            return []
        return [
            ClusterSpec(
                label=_singleton_label(ci),
                item_ids=(ci.id,),
            )
            for ci in items
        ]

    # ------------------------------------------------------------------
    # Stage 5 helper — umbrella inciting summary
    # ------------------------------------------------------------------

    def umbrella_summary(
        self, run_metadata: dict[str, Any],
        items: list[CapturedItem] | None = None,
    ) -> dict[str, Any]:
        item_count = run_metadata.get("item_count", 0)
        scan_id = run_metadata.get("scan_id")

        # Compute distinct sender count from items when we have them —
        # makes the title more informative ("Email triage: 12 unread
        # from 4 senders") than item_count alone.
        sender_count = None
        if items:
            sender_count = count_unique_senders(items)

        if sender_count and item_count:
            title = (
                f"Email triage: {item_count} unread from "
                f"{sender_count} sender(s)"
            )
        elif item_count:
            title = f"Email triage: {item_count} unread"
        else:
            title = "Email triage: nothing pending"

        return {
            "source": self.name,
            "title": title,
            "description": title,
            "item_count": item_count,
            "sender_count": sender_count,
            "scan_id": scan_id,
            "source_pipeline": "email_triage",
        }


# ---------------------------------------------------------------------------
# Helper exposed for tests (sender-count for umbrella_summary inputs)
# ---------------------------------------------------------------------------


def count_unique_senders(items: list[CapturedItem]) -> int:
    """Count distinct sender domains across a CapturedItem list.

    Exposed so ``run_source_pipeline``-style callers can pre-compute
    the sender count and pass it via run_metadata to
    :meth:`EmailTriagePipeline.umbrella_summary`. Counts by domain
    (not full sender string) because the same individual posting from
    multiple addresses on one domain should still register as one
    "sender" for the title.
    """
    domains = Counter()
    for ci in items:
        d = _domain_of((ci.payload or {}).get("sender") or "")
        if d:
            domains[d] += 1
    return len(domains)
