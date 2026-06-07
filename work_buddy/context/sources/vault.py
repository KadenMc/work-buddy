"""``vault`` context source — contract-relevant notes via the native vault index.

Wraps :func:`work_buddy.collectors.vault_collector.collect` as a
``ContextSource`` for the context pipeline. Runs natively against the
disk-backed vault index, independent of Obsidian.
"""
from __future__ import annotations

from typing import Any

from work_buddy.context import registry as _registry
from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class VaultSource(MarkdownCollectorSource):
    name = "vault"
    _heading = "Contract-Relevant Vault Content"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import vault_collector
            self._collect_fn = vault_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(VaultSource())
