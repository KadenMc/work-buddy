"""Demonstration: ``except SomeExceptionClass:`` is identity-based on
the class object, so two structurally-identical classes with the same
name don't catch each other's instances.

This is the empirical proof for the bubble-raw bug pattern observed in
the activity ledger for ``project_create`` (and other
manually-raised-ConsentRequired ops): the gateway's typed
``except ConsentRequired:`` handler at
work_buddy/mcp_server/tools/gateway.py:1535 captures the class object
at module-import time.  After ``mcp_registry_reload`` purges
``sys.modules`` and the raising code path re-imports a FRESH
``ConsentRequired`` class, ``isinstance`` against the stale class
reference returns False, the typed handler is skipped, and the
exception falls through to the broad ``except Exception:`` clause
that produces the double-prefix ``"Execution failed: ConsentRequired:
ConsentRequired: ..."`` pattern.

Same root-cause family as the registry's stale-Capability bug
(fixed defensively for ``_entry_to_dict``).  The mechanism is general:
any captured class reference is invalidated by ``sys.modules`` purges.

The test demonstrates the mechanism with two locally-defined classes
named ``ConsentRequired`` — no ``sys.modules`` mutation, no test-
suite-wide pollution.  An earlier version of this test did purge
``sys.modules`` to mirror the production trigger; restoring the
purged modules in a ``finally`` block was insufficient because the
``@requires_consent`` decorators in dependent modules (e.g.
``work_buddy.obsidian.bridge``) had already executed against the
pre-purge consent module and the freshly-imported one started with
an empty ``_CONSENT_REGISTRY``.  Subsequent tests that asserted on
that registry then spuriously failed.  The class-identity mechanism
under test does not require sys.modules mutation to demonstrate.
"""

from __future__ import annotations


def test_consent_required_isinstance_fails_across_class_redefinition() -> None:
    """``except ConsentRequired:`` is identity-based on the class object.
    Two classes with the same name and same shape, defined in different
    scopes, are different objects — and ``isinstance(exc_from_one,
    other_class)`` returns False.

    Production trigger: ``mcp_registry_reload`` purges
    ``sys.modules["work_buddy.consent"]`` and the next call to
    ``project_create`` (which lazy-imports ``ConsentRequired`` at
    context_wrappers.py:944) gets a freshly-rebuilt class object,
    different from the one the gateway captured at gateway.py:27.
    The gateway's typed handler ``except ConsentRequired:`` no longer
    matches; the exception falls through to the broad
    ``except Exception:`` clause that produces the "Execution failed:
    ConsentRequired: ConsentRequired: ..." text in the activity
    ledger.  The defensive duck-typed catch at gateway.py:1583 (added
    in the same commit) keys off ``type(exc).__name__`` instead of
    isinstance so it survives the stale-class case."""

    class ConsentRequired(Exception):
        """Stand-in for the class captured at gateway module-import time."""

        def __init__(
            self,
            operation: str,
            reason: str,
            risk: str,
            default_ttl: int,
        ) -> None:
            self.operation = operation
            self.reason = reason
            self.risk = risk
            self.default_ttl = default_ttl
            super().__init__(f"'{operation}' ({risk} risk)\nReason: {reason}")

    StaleConsentRequired = ConsentRequired

    # Re-define a structurally-identical class in a new local scope.
    # Mirrors what happens when the consent module is freshly re-imported.
    class ConsentRequired(Exception):  # noqa: F811 — deliberate shadowing
        """Stand-in for the class produced by the lazy re-import."""

        def __init__(
            self,
            operation: str,
            reason: str,
            risk: str,
            default_ttl: int,
        ) -> None:
            self.operation = operation
            self.reason = reason
            self.risk = risk
            self.default_ttl = default_ttl
            super().__init__(f"'{operation}' ({risk} risk)\nReason: {reason}")

    FreshConsentRequired = ConsentRequired

    # The two classes are different objects, but share a __name__.
    assert StaleConsentRequired is not FreshConsentRequired
    assert StaleConsentRequired.__name__ == FreshConsentRequired.__name__ == "ConsentRequired"

    # Raise an instance of the fresh class, catch with the stale class —
    # isinstance fails because it checks class identity, not name.
    try:
        raise FreshConsentRequired(
            operation="test.op",
            reason="demonstration",
            risk="low",
            default_ttl=30,
        )
    except Exception as exc:
        assert not isinstance(exc, StaleConsentRequired), (
            "Stale-class hypothesis disproved: the captured reference "
            "DID match the freshly-defined instance.  Python's `except` "
            "mechanism must have started recognising same-named classes "
            "as equivalent — re-investigate."
        )

        # Type name is identical between the two class objects, which is
        # what the gateway's defensive duck-typed catch keys off of.
        assert type(exc).__name__ == "ConsentRequired"


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
