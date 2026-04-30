"""Build per-source card-action lists for the Review UI.

The triage Review modal (dashboard tab + Obsidian dispatch) renders one
card per pending pool entry. Most cards carry a ``url`` for "open in
browser"-style sources (Chrome). Other sources (email, journal, inline)
need richer "open in app" affordances that go through an MCP capability
rather than raw browser navigation — emails, for example, should open
in Thunderbird via :func:`email_display`, not via a fake ``thunderbird:``
URL the browser doesn't understand.

This module is the **declarative seam**: each :class:`SourceDescriptor`
optionally carries an ``open_action`` config block; this helper reads the
descriptor, resolves parameters from the item's metadata, and emits the
``actions`` list the frontend renders as buttons next to the label.

Design tenets
-------------

- **Single declaration point per source.** New sources add one block to
  ``triage/sources.py``; nothing else changes.
- **Frontend stays generic.** The frontend has zero per-source
  knowledge — it just iterates ``item.actions`` and POSTs each click to
  ``/api/palette/execute`` with the supplied ``command_id`` + ``params``.
- **Defensive on every input.** Bad descriptor shapes, missing metadata
  fields, malformed param paths — all return an empty list silently. A
  broken descriptor must never break the Review tab.

Action shape (what the frontend receives)
-----------------------------------------

::

    {
        "label": "Open in Thunderbird",
        "command_id": "work-buddy::email_display",
        "params": {
            "provider_message_id": "<RFC id>",
            "folder_path": "<bridge folder URI>",
        },
        "quarantine_on_error_kinds": ["email_message_not_found"],
    }

The ``command_id`` is the dashboard's existing palette-execute prefix
(``work-buddy::<capability>``). No new HTTP endpoint; the click reuses
``POST /api/palette/execute``.

``quarantine_on_error_kinds`` (optional, list of strings): if the
action fails with one of these ``error_kind`` values, the frontend
auto-triggers ``triage_pool_quarantine_entry`` to mark the entry stale.
Self-healing UX for "user clicked, source is gone" — the click failure
becomes the evidence for quarantine without waiting for the cron sweep.

Descriptor shape (what authors write)
-------------------------------------

In ``work_buddy/triage/sources.py``::

    "email_message": {
        ...
        "config": {
            "open_action": {
                "label": "Open in Thunderbird",
                "capability": "email_display",
                "param_map": {
                    "provider_message_id": "metadata.provider_message_id",
                    "folder_path":         "metadata.folder_path",
                },
            },
        },
    }

``param_map`` values are dot-paths into the item dict (the
:meth:`TriageItem.to_dict` shape). ``metadata.<key>`` is the common
case; bare ``<key>`` resolves at the item top level (``label``, ``url``,
``id``, etc.).

Future-proofing: ``open_action`` is intentionally *one* action; the
emitted ``actions`` array is plural so future descriptor schemas can
declare multiple actions per source (e.g. ``["open", "compose_reply"]``)
without breaking the contract.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_card_actions(
    source: str,
    item: dict[str, Any],
    *,
    descriptor: Any | None = None,
) -> list[dict[str, Any]]:
    """Return the ``actions`` array for one Review-card item.

    Args:
        source: The pool entry's source string (matches
            ``TriageItem.source`` and the descriptor's name).
        item: The item dict — typically ``ClarifyEntry.item`` (the
            :meth:`TriageItem.to_dict` output). Required keys depend
            on the descriptor's ``param_map``.
        descriptor: Optional pre-resolved :class:`SourceDescriptor`.
            When omitted, looked up via :func:`get_descriptor`. Pass
            explicitly in test code or when the caller has already
            loaded it (small perf win when iterating many entries).

    Returns:
        A possibly-empty list of action dicts. Empty list when:
          - no descriptor for this source
          - descriptor has no ``open_action`` config
          - ``open_action`` is malformed (missing capability/label)
          - ``param_map`` references metadata keys that aren't on the
            item (defensive — better an absent button than a broken
            click).
    """
    if not source or not isinstance(item, dict):
        return []

    if descriptor is None:
        try:
            from work_buddy.clarify.sources import get_descriptor
            descriptor = get_descriptor(source)
        except Exception as exc:  # noqa: BLE001 — never let descriptor lookup break the UI
            logger.debug("build_card_actions: descriptor lookup failed for %r: %s", source, exc)
            return []
    if descriptor is None:
        return []

    cfg = (getattr(descriptor, "config", None) or {})
    spec = cfg.get("open_action")
    if not isinstance(spec, dict):
        return []

    capability = spec.get("capability")
    label = spec.get("label")
    if not isinstance(capability, str) or not capability:
        logger.debug("build_card_actions: %r open_action missing capability", source)
        return []
    if not isinstance(label, str) or not label:
        logger.debug("build_card_actions: %r open_action missing label", source)
        return []

    param_map = spec.get("param_map") or {}
    if not isinstance(param_map, dict):
        return []

    params: dict[str, Any] = {}
    for kwarg, path in param_map.items():
        if not isinstance(kwarg, str) or not isinstance(path, str):
            continue
        value = _resolve_path(item, path)
        if value is None or value == "":
            # Required param is missing — drop the whole action. We'd
            # rather show no button than ship a broken click.
            logger.debug(
                "build_card_actions: %r open_action missing param %r at path %r",
                source, kwarg, path,
            )
            return []
        params[kwarg] = value

    action: dict[str, Any] = {
        "label": label,
        "command_id": f"work-buddy::{capability}",
        "params": params,
    }
    qoek = spec.get("quarantine_on_error_kinds")
    if isinstance(qoek, list) and qoek:
        # Filter out non-string entries defensively; the frontend uses
        # this list for direct equality checks against response.error_kind.
        kinds = [k for k in qoek if isinstance(k, str) and k]
        if kinds:
            action["quarantine_on_error_kinds"] = kinds
    return [action]


def has_open_action(source: str, *, descriptor: Any | None = None) -> bool:
    """True iff this source declares an ``open_action``.

    Used by the presentation builder to suppress the legacy ``url``
    field on items whose source has opted into the new actions seam —
    the URL was almost always a synthetic marker (``thunderbird:msg/…``)
    that didn't navigate anywhere useful, and the action button is the
    real "open in app" affordance.
    """
    if not source:
        return False
    if descriptor is None:
        try:
            from work_buddy.clarify.sources import get_descriptor
            descriptor = get_descriptor(source)
        except Exception:
            return False
    if descriptor is None:
        return False
    cfg = (getattr(descriptor, "config", None) or {})
    spec = cfg.get("open_action")
    return isinstance(spec, dict) and bool(spec.get("capability"))


def _resolve_path(item: dict[str, Any], path: str) -> Any:
    """Look up a dot-path in an item dict.

    ``"metadata.provider_message_id"`` walks ``item["metadata"]["provider_message_id"]``.
    ``"label"`` walks ``item["label"]``. Returns ``None`` on any miss
    (missing key, non-dict intermediate, malformed path).
    """
    if not path:
        return None
    parts = path.split(".")
    current: Any = item
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current
