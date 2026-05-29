"""Demonstration: ConsentRequired class identity does not survive
mcp_registry_reload's sys.modules purge.

This is the empirical proof for the bubble-raw bug pattern observed in
the activity ledger for project_create (and other manually-raised
ConsentRequired ops): the gateway's typed ``except ConsentRequired:``
handler at work_buddy/mcp_server/tools/gateway.py:1535 captures the
class object at module-import time. After mcp_registry_reload purges
sys.modules and the raising code path re-imports a FRESH
ConsentRequired class, isinstance against the stale class reference
returns False, so the typed handler is skipped and the exception
falls through to the broad ``except Exception:`` clause that produces
the double-prefix ``"Execution failed: ConsentRequired: ConsentRequired:
..."`` pattern.

Same root-cause family as the registry's stale-Capability bug
(fixed defensively in commit 83d595aa for _entry_to_dict). The
mechanism is general: any captured class reference is invalidated
by sys.modules purges.
"""

from __future__ import annotations

import importlib
import sys


def test_consent_required_isinstance_fails_after_module_reload() -> None:
    """When work_buddy.consent is purged from sys.modules and re-imported,
    the resulting ConsentRequired class is a DIFFERENT object than any
    reference captured before the purge. isinstance checks against
    the stale reference return False, even though the raised exception
    semantically IS a ConsentRequired.

    This is the mechanism that explains the activity ledger's
    "Execution failed: ConsentRequired: ConsentRequired:" pattern
    for project_create: the gateway's captured ``ConsentRequired`` (from
    its module-level ``from work_buddy.consent import ConsentRequired``
    on gateway.py:27) is the stale class; project_create's lazy import
    (context_wrappers.py:944) returns the FRESH class; the raised
    instance does not match the stale ``except`` clause."""

    # Capture the pre-reload class reference. This mimics gateway.py's
    # module-level ``from work_buddy.consent import ConsentRequired``.
    from work_buddy.consent import ConsentRequired as StaleConsentRequired

    # Purge work_buddy.consent (and dependent submodules) from
    # sys.modules. This is what mcp_registry_reload does as part of
    # its invalidate-and-rebuild cycle.
    purged_modules = [
        name for name in list(sys.modules)
        if name == "work_buddy.consent" or name.startswith("work_buddy.consent.")
    ]
    for name in purged_modules:
        del sys.modules[name]

    try:
        # Re-import. This mimics project_create's lazy import
        # (context_wrappers.py:944's ``from work_buddy.consent import
        # ConsentRequired``) running AFTER the gateway captured its
        # reference.
        import work_buddy.consent as fresh_consent_module
        FreshConsentRequired = fresh_consent_module.ConsentRequired

        # Raise + catch the fresh class. This is what context_wrappers's
        # project_create does internally: raise the freshly-imported
        # ConsentRequired.
        try:
            raise FreshConsentRequired(
                operation="test.op",
                reason="demonstration",
                risk="low",
                default_ttl=30,
            )
        except Exception as exc:
            # The stale class reference fails isinstance against the
            # fresh class's instance. This is the bubble-raw mechanism:
            # the gateway's `except StaleConsentRequired:` would NOT
            # catch this exception. It falls through to the broad
            # `except Exception:` instead, producing the double-prefix
            # "Execution failed: ConsentRequired: ConsentRequired:..."
            # text we see in the activity ledger.
            assert not isinstance(exc, StaleConsentRequired), (
                "Stale-class hypothesis disproved: the captured reference "
                "DID match the freshly-imported instance. The bubble-raw "
                "pattern for project_create must have a different cause."
            )

            # And confirm the two classes are not the same object —
            # the actual class identity mismatch that drives the
            # isinstance failure.
            assert StaleConsentRequired is not FreshConsentRequired, (
                "Classes are the same object — sys.modules purge did "
                "not actually re-create the class."
            )

            # Type name is identical between the two class objects —
            # which is what the gateway's defensive duck-typed catch
            # (added at gateway.py:1583 broad-except fallback) keys
            # off of to route the exception correctly when isinstance
            # fails.
            assert type(exc).__name__ == "ConsentRequired"
            # The exception message does NOT self-prefix with the type
            # name (see test_no_redundant_self_prefix_in_exception_message).
            assert not str(exc).startswith("ConsentRequired:")

    finally:
        # Restore module state so this test doesn't poison the rest of
        # the suite. Re-importing all originally-purged modules.
        for name in purged_modules:
            if name not in sys.modules:
                importlib.import_module(name)


def test_no_redundant_self_prefix_in_exception_message() -> None:
    """``ConsentRequired.__init__`` must NOT prefix its own message with
    ``"ConsentRequired:"`` — the class name is always available via
    ``type(exc).__name__`` for any caller that wants it.

    Background — the bug this guards against: the activity ledger
    captured ``"Execution failed: ConsentRequired: ConsentRequired:
    'project_create' ..."`` as the error text for bubble-raw events. The
    double prefix arose because (1) the exception self-prefixed its
    message, AND (2) the gateway's broad-Exception path serialised via
    ``f"{type(exc).__name__}: {exc}"`` — re-adding the class name a
    second time.

    Fix: drop the self-prefix.  ``f"{type(exc).__name__}: {exc}"`` now
    produces a single clean ``"ConsentRequired: '<op>' (<risk>) ..."``
    prefix even when the broad-Exception fallback is taken (e.g.
    because of the stale-class issue covered above)."""
    from work_buddy.consent import ConsentRequired

    exc = ConsentRequired(
        operation="test.op",
        reason="demonstration",
        risk="low",
        default_ttl=30,
    )

    # The exception's own string starts with the operation, not the
    # type name.
    assert not str(exc).startswith("ConsentRequired"), (
        "Self-prefix has regressed.  ConsentRequired.__init__ must not "
        "prepend the class name to its message — see the double-prefix "
        "bubble-raw bug documented in the activity ledger."
    )
    assert str(exc).startswith("'test.op'")

    # The gateway's broad-Exception stringification now produces a
    # single clean prefix.
    error_str = f"{type(exc).__name__}: {exc}"
    assert error_str.startswith("ConsentRequired: 'test.op' (low risk)")
    # And critically — no double prefix anywhere in the string.
    assert "ConsentRequired: ConsentRequired" not in error_str

    # And the dispatcher wrap stays clean too.
    final = f"Execution failed: {error_str}"
    assert final.startswith("Execution failed: ConsentRequired: 'test.op'")
    assert "ConsentRequired: ConsentRequired" not in final
