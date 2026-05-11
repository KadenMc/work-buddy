"""Regression test for the bundle-unbundling contract in
``resolve_consent_request``.

When the gateway's auto-consent flow asks the user for consent on a
capability that touches multiple ops (e.g. ``task_create`` needs both
``tasks.create_task`` and ``obsidian.write_file``), the bundled
notification's operation key is ``bundle:<capability_name>`` — a
notification label, not a real operation any decorator checks. The
list of underlying ops travels in
``consent_meta["context"]["operations"]``.

``resolve_consent_request`` must grant each underlying op individually
in addition to the bundle label so the ``@requires_consent`` gates
actually pass on the next call. Without this, the gateway's
``grant_consent_batch`` is the only thing keeping the in-window path
working, and out-of-band approvals that go through ``resolve_consent_request``
directly (the sidecar's consent_grant message handler) would write
only the bundle label.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


_test_counter = 0


def _setup_temp_session() -> str:
    """Same isolation primitive used by test_consent_auto_request.py."""
    global _test_counter
    _test_counter += 1
    td = tempfile.mkdtemp()
    os.environ["WORK_BUDDY_SESSION_ID"] = (
        f"test-consent-bundle-{_test_counter}-{os.getpid()}"
    )

    import work_buddy.agent_session as asmod
    asmod.get_agents_dir = lambda: Path(td)
    asmod._cached_session_dir = None

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    return td


def test_resolve_consent_request_unbundles_operations() -> None:
    """Approving a bundle: notification grants every individual op
    declared in ``consent_meta.context.operations``.
    """
    _setup_temp_session()
    from work_buddy.consent import (
        create_consent_request,
        resolve_consent_request,
        _cache,
    )

    record = create_consent_request(
        operation="bundle:probe_capability",
        reason="probe — two underlying ops",
        risk="moderate",
        default_ttl=15,
        requester="test:bug1b",
        context={
            "capability": "probe_capability",
            "operations": ["probe.op_a", "probe.op_b"],
            "operation_id": "op_probe_xx",
        },
    )

    resolve_consent_request(
        record["request_id"], approved=True, mode="once",
    )

    # Every operation in `context.operations` must get its own grant
    # row — that's what the `@requires_consent` decorators check.
    assert _cache.is_granted("probe.op_a"), (
        "individual op `probe.op_a` should be granted after approving "
        "the bundled notification"
    )
    assert _cache.is_granted("probe.op_b"), (
        "individual op `probe.op_b` should be granted after approving "
        "the bundled notification"
    )


def test_resolve_consent_request_non_bundle_unchanged() -> None:
    """Single-operation consent (no bundle, no `context.operations`)
    still grants exactly the named op — no regression."""
    _setup_temp_session()
    from work_buddy.consent import (
        create_consent_request,
        resolve_consent_request,
        _cache,
    )

    record = create_consent_request(
        operation="probe.single_op",
        reason="probe — single op",
        risk="low",
        default_ttl=5,
        requester="test:bug1b",
        context=None,
    )

    resolve_consent_request(
        record["request_id"], approved=True, mode="temporary",
        ttl_minutes=10,
    )

    assert _cache.is_granted("probe.single_op")


def test_resolve_consent_request_deny_does_not_unbundle() -> None:
    """Denial path doesn't grant anything, bundle or otherwise."""
    _setup_temp_session()
    from work_buddy.consent import (
        create_consent_request,
        resolve_consent_request,
        _cache,
    )

    record = create_consent_request(
        operation="bundle:probe_deny",
        reason="probe — denial",
        risk="moderate",
        default_ttl=5,
        requester="test:bug1b",
        context={"operations": ["probe.deny_a", "probe.deny_b"]},
    )

    resolve_consent_request(record["request_id"], approved=False)

    assert not _cache.is_granted("probe.deny_a")
    assert not _cache.is_granted("probe.deny_b")
    assert not _cache.is_granted("bundle:probe_deny")
