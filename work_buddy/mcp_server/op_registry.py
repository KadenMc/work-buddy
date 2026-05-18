"""Op registry — a stable-ID lookup table for executable callables.

An **Op** is the executable half of a capability: a Python callable registered
under a stable ``op.<namespace>.<name>`` identifier. A **capability
declaration** (an inert knowledge-store unit of ``kind: "capability"``) carries
an ``op`` field naming an Op; the capability loader resolves that reference at
registry-build time. This mirrors how a workflow references a capability by
name — see ``work_buddy/knowledge/capability_loader.py``.

This module is Core mechanism: it holds no domain opinion, only a dict keyed by
op ID. Built-in ops live in ``work_buddy/mcp_server/ops/``; ``load_builtin_ops``
imports that package so each module registers its ops as an import side effect.

``mcp_registry_reload`` purges ``work_buddy.*`` from ``sys.modules``, so this
module — and ``_OPS`` with it — is re-imported fresh on every reload; there is
no stale state to clear.
"""

from __future__ import annotations

import logging
from typing import Callable

import re

logger = logging.getLogger(__name__)

# op.<namespace>.<name> — the namespace is a single lowercase segment; the name
# may itself be dotted, so reverse-DNS third-party IDs (op.dev.alice.email_send)
# validate too. Built-in ops use the short ``wb`` namespace (op.wb.task_read).
OP_ID_RE = re.compile(r"^op\.[a-z0-9]+(?:\.[a-z0-9_]+)+$")

_OPS: dict[str, Callable] = {}
_builtins_loaded = False


def is_valid_op_id(op_id: str) -> bool:
    """Return True if ``op_id`` matches the ``op.<namespace>.<name>`` grammar."""
    return isinstance(op_id, str) and bool(OP_ID_RE.match(op_id))


def register_op(op_id: str, fn: Callable, *, replace: bool = False) -> None:
    """Register an executable callable under a stable op ID.

    Raises ``ValueError`` on a malformed ID, a non-callable target, or a
    duplicate registration (unless ``replace=True``).
    """
    if not is_valid_op_id(op_id):
        raise ValueError(
            f"Invalid op ID {op_id!r} — expected 'op.<namespace>.<name>' with "
            "lowercase alphanumerics and underscores (e.g. 'op.wb.task_read')."
        )
    if not callable(fn):
        raise ValueError(f"Op {op_id!r} target is not callable: {fn!r}")
    if op_id in _OPS and not replace:
        raise ValueError(
            f"Op {op_id!r} is already registered. Pass replace=True to override."
        )
    _OPS[op_id] = fn


def get_op(op_id: str) -> Callable | None:
    """Return the callable registered under ``op_id``, or None if unregistered."""
    return _OPS.get(op_id)


def list_ops() -> list[str]:
    """Return every registered op ID, sorted."""
    return sorted(_OPS)


def clear_ops() -> None:
    """Drop all registered ops. For test isolation and reload cleanliness."""
    global _builtins_loaded
    _OPS.clear()
    _builtins_loaded = False


def load_builtin_ops() -> None:
    """Import the built-in ops package so each module registers its ops.

    Idempotent within a process: a module guard runs the import side effects
    once. If a prior ``clear_ops`` reset the guard while the op modules are
    still cached in ``sys.modules``, they are reloaded so registration re-runs.
    """
    global _builtins_loaded
    if _builtins_loaded:
        return

    import importlib
    import pkgutil
    import sys

    from work_buddy.mcp_server import ops as _ops_pkg

    for mod in pkgutil.iter_modules(_ops_pkg.__path__):
        full_name = f"{_ops_pkg.__name__}.{mod.name}"
        if full_name in sys.modules:
            importlib.reload(sys.modules[full_name])
        else:
            importlib.import_module(full_name)

    _builtins_loaded = True
    logger.debug("Built-in ops loaded: %d registered", len(_OPS))
