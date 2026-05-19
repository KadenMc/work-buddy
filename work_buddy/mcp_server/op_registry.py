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
# Effect manifests keyed by op ID. An ``effects`` manifest is a list of
# ``EffectSpec`` objects — code, not data (an ``EffectSpec`` may carry a
# ``resolver`` callable), so it cannot live in a data declaration. A capability
# declaration that needs effects names its op; the op module registers the
# manifest here, and the capability loader threads it onto the resolved
# ``Capability``. Empty for the overwhelming majority of capabilities.
_OP_EFFECTS: dict[str, list] = {}
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


def register_op_effects(op_id: str, effects: list) -> None:
    """Register an effect manifest for an op.

    Effects are code (an ``EffectSpec`` may hold a ``resolver`` callable), so
    they cannot ride in a data declaration — the op module registers them here
    and the capability loader threads them onto the resolved ``Capability``.
    """
    if not is_valid_op_id(op_id):
        raise ValueError(f"Invalid op ID {op_id!r} for effect registration.")
    _OP_EFFECTS[op_id] = list(effects)


def get_op_effects(op_id: str) -> list:
    """Return the effect manifest registered for ``op_id`` (empty if none)."""
    return _OP_EFFECTS.get(op_id, [])


def clear_ops() -> None:
    """Drop all registered ops. For test isolation and reload cleanliness."""
    global _builtins_loaded
    _OPS.clear()
    _OP_EFFECTS.clear()
    _builtins_loaded = False


def load_builtin_ops() -> None:
    """Import the built-in ops package so each module registers its ops.

    Idempotent within a process: a module guard runs the import side effects
    once. If a prior ``clear_ops`` reset the guard while the op modules are
    still cached in ``sys.modules``, they are reloaded so registration re-runs.

    A per-module failure (an optional dependency the host environment lacks,
    e.g. ``hindsight_client`` on CI) is logged and skipped; remaining modules
    still register. This mirrors how the legacy capability builders were
    guarded with try/except inside ``_build_registry``.
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
        try:
            if full_name in sys.modules:
                importlib.reload(sys.modules[full_name])
            else:
                importlib.import_module(full_name)
        except Exception as exc:
            logger.warning(
                "Op module %s failed to load (%s: %s); skipping its ops.",
                full_name, type(exc).__name__, exc,
            )

    _builtins_loaded = True
    logger.debug("Built-in ops loaded: %d registered", len(_OPS))
