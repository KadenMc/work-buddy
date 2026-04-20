"""Tests for inline watcher reconciliation."""

from __future__ import annotations

import pytest

from work_buddy.inline import registry, store, sync


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    snap = dict(registry._COMMANDS)  # noqa: SLF001
    registry._COMMANDS.clear()  # noqa: SLF001

    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "inline.db")
    c = store.get_connection()
    store._ensure_schema(c)  # noqa: SLF001
    c.close()
    try:
        yield
    finally:
        registry._COMMANDS.clear()  # noqa: SLF001
        registry._COMMANDS.update(snap)  # noqa: SLF001


def _make_vault(hits):
    """Return a stub for _vault_tags_for_command."""
    def _stub(cmd_name):
        return hits.get(cmd_name, [])
    return _stub


def test_sync_adds_new_watchers(monkeypatch):
    @registry.inline_command(name="ping", surfaces=["tag"], persistent=True)
    def h(ctx):
        return None

    monkeypatch.setattr(
        sync,
        "_vault_tags_for_command",
        _make_vault(
            {
                "ping": [
                    {"file_path": "Notes/A.md", "tag": "wb/cmd/ping", "tag_line": None},
                    {"file_path": "Notes/B.md", "tag": "wb/cmd/ping", "tag_line": None},
                ]
            }
        ),
    )

    out = sync.inline_sync()
    assert len(out["added"]) == 2
    assert out["removed"] == []
    assert len(store.list_watchers(command_name="ping")) == 2


def test_sync_removes_stale_watchers(monkeypatch):
    @registry.inline_command(name="ping", surfaces=["tag"], persistent=True)
    def h(ctx):
        return None

    # Seed a watcher that doesn't appear in vault
    store.create_watcher(
        command_name="ping",
        file_path="Notes/Gone.md",
        tag="wb/cmd/ping",
    )
    monkeypatch.setattr(sync, "_vault_tags_for_command", _make_vault({"ping": []}))

    out = sync.inline_sync()
    assert out["added"] == []
    assert len(out["removed"]) == 1
    assert store.list_watchers(command_name="ping") == []


def test_sync_ignores_non_persistent(monkeypatch):
    @registry.inline_command(name="once", surfaces=["tag"], persistent=False)
    def h(ctx):
        return None

    called = {"n": 0}

    def _stub(cmd_name):
        called["n"] += 1
        return []

    monkeypatch.setattr(sync, "_vault_tags_for_command", _stub)
    sync.inline_sync()
    assert called["n"] == 0  # non-persistent command skipped
