"""The legacy Tasks tab becomes a doorway after native activation."""

from __future__ import annotations

from work_buddy.dashboard import frontend


def test_render_page_exposes_inactive_native_epoch(monkeypatch):
    monkeypatch.setattr(frontend, "_native_tasks_active", lambda: False)

    page = frontend.render_page()

    assert 'data-native-tasks="false"' in page
    assert 'id="master-task-link"' in page
    assert 'id="task-sync-btn"' in page


def test_render_page_and_bundle_redirect_native_tasks(monkeypatch):
    monkeypatch.setattr(frontend, "_native_tasks_active", lambda: True)

    page = frontend.render_page()
    javascript = frontend.assembled_js()

    assert 'data-native-tasks="true"' in page
    assert 'href="/app/tasks"' in page
    assert 'id="master-task-link"' not in page
    assert 'id="task-sync-btn"' not in page
    assert "master-task-list.md" not in page
    assert "tabName === 'tasks' && WB_NATIVE_TASKS_ACTIVE" in javascript
    assert "window.location.assign('/app/tasks')" in javascript
    assert "!WB_NATIVE_TASKS_ACTIVE && WB_VAULT_NAME" in javascript


def test_authority_failure_routes_away_from_legacy_tasks(monkeypatch):
    from work_buddy.tasks import runtime

    def unavailable():
        raise RuntimeError("authority state cannot be reconciled")

    monkeypatch.setattr(runtime, "native_authority_active", unavailable)

    page = frontend.render_page()

    assert 'data-native-tasks="true"' in page
    assert 'id="master-task-link"' not in page
    assert 'id="task-sync-btn"' not in page
