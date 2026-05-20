"""Capability loader — resolves inert capability declarations against ops.

A **capability declaration** is a knowledge-store unit of ``kind: "capability"``
that carries an ``op`` field naming an Op ID. This module reads those
declarations, resolves each ``op`` against the Op registry, validates the
declared parameter schema against the resolved callable's signature, and emits
ready-to-dispatch ``Capability`` objects the gateway registry can hold.

It is the data-first counterpart to ``_discover_workflows_from_store()``:
workflows are inert data the conductor resolves at load time; capability
declarations load the same way. Every capability unit is a declaration,
discriminated by a non-empty ``op`` field.

See ``architecture/data-first-capabilities``.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# The version of the declaration *format* this loader understands. Bumped only
# when the shape of a capability unit changes incompatibly; the loader refuses
# declarations stamped with an unknown version so old data fails loud, not weird.
SCHEMA_VERSION = "wb-capability/v1"

# Issue dicts share the shape the validator's check registry expects
# (``check`` / ``path`` / ``message``), plus a ``severity``. They carry
# ``warning`` severity: the direct and declaration-based registration paths
# coexist, so an unresolved declaration is surfaced without blocking the store.
_CHECK_NAME = "capability_op_resolution"


def _issue(path: str, message: str) -> dict[str, str]:
    return {
        "check": _CHECK_NAME,
        "path": path,
        "message": message,
        "severity": "warning",
    }


def validate_signature(declared_params: dict[str, Any], fn: Callable) -> list[str]:
    """Compare a declared parameter schema against a callable's signature.

    Returns a list of human-readable mismatch messages (empty == match). A
    callable that accepts ``**kwargs`` is treated as accepting any declared
    parameter name. Callables whose signature cannot be introspected (some
    C-level builtins) are treated as matching — there is nothing to check.

    The check covers parameter *names* and *required-ness*, not type strings.
    """
    try:
        sig = inspect.signature(fn, follow_wrapped=True)
    except (ValueError, TypeError):
        return []

    issues: list[str] = []
    named = {
        name: p
        for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                       inspect.Parameter.KEYWORD_ONLY)
    }
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    # Every declared parameter must be a parameter the callable accepts.
    for pname in declared_params:
        if pname not in named and not accepts_kwargs:
            issues.append(
                f"declared parameter {pname!r} is not accepted by the op callable"
            )

    # Every parameter the callable *requires* must be present in the
    # declaration — otherwise a caller relying on the declared schema can omit
    # a parameter the op cannot run without.
    for pname, p in named.items():
        if p.default is inspect.Parameter.empty and pname not in declared_params:
            issues.append(
                f"op callable requires parameter {pname!r} but the declaration "
                "does not declare it"
            )

    return issues


def load_declared_capabilities(
    store: dict[str, Any] | None = None,
) -> tuple[list[Any], list[dict[str, str]]]:
    """Resolve every declaration-based capability unit in the knowledge store.

    Args:
        store: A pre-loaded knowledge store to resolve against. Defaults to
            ``load_store()`` — passing one explicitly is for tests.

    Returns ``(capabilities, issues)``:
      - ``capabilities`` — resolved ``Capability`` objects ready for the
        gateway registry. A declaration that fails resolution is *omitted*
        (never dispatched against a broken op) rather than raising.
      - ``issues`` — warning dicts for unknown schema versions, malformed or
        missing op references, and signature mismatches.
    """
    from work_buddy.knowledge.model import CapabilityUnit
    from work_buddy.knowledge.store import load_store
    from work_buddy.mcp_server import op_registry
    from work_buddy.mcp_server.registry import Capability

    op_registry.load_builtin_ops()
    if store is None:
        store = load_store()

    capabilities: list[Any] = []
    issues: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if not isinstance(unit, CapabilityUnit) or not unit.op:
            continue  # not a declaration-based capability

        if unit.schema_version != SCHEMA_VERSION:
            issues.append(_issue(
                path,
                f"unknown schema_version {unit.schema_version!r} — this loader "
                f"supports {SCHEMA_VERSION!r}",
            ))
            continue

        if not op_registry.is_valid_op_id(unit.op):
            issues.append(_issue(path, f"malformed op ID {unit.op!r}"))
            continue

        fn = op_registry.get_op(unit.op)
        if fn is None:
            issues.append(_issue(
                path, f"op {unit.op!r} is not registered in the Op registry"
            ))
            continue

        if not unit.capability_name:
            issues.append(_issue(path, "declaration is missing 'capability_name'"))
            continue

        sig_issues = validate_signature(unit.parameters, fn)
        if sig_issues:
            for si in sig_issues:
                issues.append(_issue(path, f"signature mismatch: {si}"))
            continue  # never dispatch a capability whose schema disagrees with its op

        capabilities.append(Capability(
            name=unit.capability_name,
            description=unit.description,
            category=unit.category,
            parameters=unit.parameters,
            callable=fn,
            search_aliases=list(unit.aliases),
            param_aliases=dict(unit.param_aliases),
            requires=list(unit.requires),
            invokes=list(unit.invokes),
            mutates_state=unit.mutates_state,
            retry_policy=unit.retry_policy,
            auto_retry=unit.auto_retry,
            slash_command=unit.slash_command or None,
            consent_operations=list(unit.consent_operations),
            effects=op_registry.get_op_effects(unit.op),
            op_id=unit.op,
            is_action=unit.is_action,
            intrinsic_amplifiers=dict(unit.intrinsic_amplifiers),
        ))

    return capabilities, issues
