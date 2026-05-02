"""Shared fixtures for v5 Thread tests.

Auto-applied isolation: stops `resolution_surface.publish()` from
writing real Notification records to the live consent-requests
directory while tests are running. Several tests walk real FSM
transitions whose state-entry handlers call publish; without this
fixture, every transition into a wait state side-effected a
`resolution_*.json` into the user's consent dir, polluting the
dashboard's notification list.

Tests that want to *capture* published Resolution Requests can
still do so via ``patch.object(resolution_surface, "publish",
side_effect=...)`` inside the test body — the inner patch takes
precedence over the autouse no-op.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_resolution_publish(monkeypatch):
    """Replace ``resolution_surface.publish`` with a no-op for the
    duration of every threads-namespace test. Prevents pollution
    of the live notifications directory."""
    from work_buddy.threads import resolution_surface
    monkeypatch.setattr(
        resolution_surface, "publish",
        lambda rr: None,
    )
