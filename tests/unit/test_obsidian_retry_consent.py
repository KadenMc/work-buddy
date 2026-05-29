"""obsidian_retry must propagate ConsentRequired to the gateway, not
swallow it into a result dict.

Background — the bug that motivated this test: the activity ledger
captured 31 historical events of obsidian_retry returning a result
dict whose ``error`` field was the raw ``ConsentRequired: ...``
exception text. The mechanism was the retry loop's broad
``except Exception:`` (work_buddy/obsidian/retry.py:597) treating
ConsentRequired identically to any non-transient failure: it would
classify (ConsentRequired is "permanent" via _PERMANENT_EXCEPTION_NAMES,
errors.py:91), see non-transient, and return
``{"success": False, "error": str(exc)}``.

The fix added an explicit ``except ConsentRequired: raise`` before the
broad ``except Exception:`` so the exception propagates to the
gateway's typed handler (gateway.py:1535) which can call
``_auto_consent_request`` and actually issue the prompt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from work_buddy.obsidian.retry import obsidian_retry


class _FakeEntry:
    def __init__(self, callable_):
        self.callable = callable_


def _raise_consent_required(**_kwargs: Any) -> None:
    # Lazy import to mirror production code paths and to survive other
    # tests in this directory that purge ``sys.modules`` to exercise the
    # stale-class hazard.  Without a lazy import here, this helper would
    # raise a stale ConsentRequired class while ``obsidian.retry``'s
    # internal lazy import would catch against the fresh class — and the
    # test would spuriously fail on test-suite ordering effects.
    from work_buddy.consent import ConsentRequired
    raise ConsentRequired(
        operation="fake.protected_op",
        reason="Demonstration that obsidian_retry swallows this exception type",
        risk="low",
        default_ttl=30,
    )


def test_obsidian_retry_propagates_consent_required() -> None:
    """obsidian_retry must re-raise ConsentRequired so the gateway's
    typed ConsentRequired handler (gateway.py:1535) can drive
    _auto_consent_request. If this test fails — meaning ConsentRequired
    was swallowed into a result dict — the bug-raw pattern from the
    activity ledger has regressed."""
    # Lazy import — see note on _raise_consent_required above for why
    # the module-level import would be unsafe in this test directory.
    from work_buddy.consent import ConsentRequired as CurrentConsentRequired

    fake_op = {
        "operation_id": "op_test",
        "name": "fake.protected_op",
        "params": {},
    }
    fake_entry = _FakeEntry(_raise_consent_required)

    # obsidian_retry uses lazy imports inside its function body — patch
    # at the SOURCE modules so the lazy imports resolve to the patches.
    with patch(
        "work_buddy.mcp_server.tools.gateway._load_operation",
        return_value=fake_op,
    ), patch(
        "work_buddy.mcp_server.registry.get_registry",
        return_value={"fake.protected_op": fake_entry},
    ), patch(
        "work_buddy.obsidian.bridge.is_available",
        return_value=True,
    ):
        with pytest.raises(CurrentConsentRequired) as exc_info:
            obsidian_retry(
                operation_id="op_test",
                max_retries=1,
                wait_seconds=0,
            )

    # The propagated exception carries the inner operation's identity
    # so the gateway's outer handler can pass it to _auto_consent_request.
    assert exc_info.value.operation == "fake.protected_op"


def test_consent_required_classifies_as_permanent() -> None:
    """Supporting evidence: ConsentRequired is classified as 'permanent'
    via _PERMANENT_EXCEPTION_NAMES (errors.py:91). That correctly tells
    the retry queue "don't auto-retry this" — a consent gate isn't a
    transport hiccup. But obsidian_retry's failure handler treats
    everything non-transient identically: it returns the error string
    as a result dict instead of routing to the consent flow.

    The fix is at the obsidian_retry layer, NOT at classify_error.
    classify_error has the right answer; obsidian_retry is asking the
    wrong question (`is this transient?`) when what it needs is `is
    this a consent gate?`.
    """
    from work_buddy.consent import ConsentRequired
    from work_buddy.errors import classify_error

    exc = ConsentRequired(
        operation="x",
        reason="y",
        risk="low",
        default_ttl=30,
    )
    assert classify_error(exc) == "permanent"
