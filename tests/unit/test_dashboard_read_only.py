"""Storage and request boundaries for the read-only dashboard process."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from work_buddy.dashboard import read_only
from work_buddy.storage import read_only as storage_posture


@pytest.fixture
def sqlite_boundary(monkeypatch):
    monkeypatch.setattr(storage_posture, "_enabled", False)
    monkeypatch.setattr(sqlite3, "connect", read_only._connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", read_only._connect)
    read_only.install_sqlite_read_only()


@pytest.mark.parametrize("style", ["path", "bytes", "uri", "positional", "factory"])
def test_all_disk_connection_forms_are_read_only(tmp_path, monkeypatch, style):
    path = tmp_path / "space # and & authority.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (name TEXT)")
    monkeypatch.setattr(storage_posture, "_enabled", False)
    monkeypatch.setattr(sqlite3, "connect", read_only._connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", read_only._connect)
    read_only.install_sqlite_read_only()
    database = {"path": path, "bytes": bytes(path), "uri": path.as_uri() + "?mode=rwc&cache=private"}.get(style, str(path))
    if style == "positional":
        connection = sqlite3.connect(database, 5, 0, None, True, sqlite3.Connection, 128, False)
    elif style == "factory":
        class Connection(sqlite3.Connection):
            pass
        connection = sqlite3.connect(database, factory=Connection)
        assert isinstance(connection, Connection)
    else:
        connection = sqlite3.connect(database)
    with connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute("INSERT INTO items VALUES ('forbidden')")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("ATTACH DATABASE ? AS other", (str(tmp_path / "other.db"),))
    connection.close()
    assert not (tmp_path / "other.db").exists()


@pytest.mark.parametrize("database", [":memory:", "file::memory:?cache=shared", "file:memory-test?mode=memory&cache=shared", ""])
def test_memory_databases_are_not_rewritten_to_disk(database, sqlite_boundary):
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_task_store_reads_without_migration_and_refuses_writes(tmp_path, monkeypatch):
    from work_buddy.tasks.store import TaskStore
    store = TaskStore(tmp_path / "tasks.db")
    store.initialize()
    monkeypatch.setattr(storage_posture, "_enabled", False)
    monkeypatch.setattr(sqlite3, "connect", read_only._connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", read_only._connect)
    read_only.install_sqlite_read_only()
    with store.connect() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute("CREATE TABLE forbidden (id TEXT)")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY


def test_chokepoint_refuses_non_safe_before_other_hooks(monkeypatch):
    app = Flask(__name__)
    app.extensions["dashboard_read_only"] = lambda: True
    app.before_request(read_only.guard_dashboard_request)
    calls = []
    app.before_request(lambda: calls.append("domain lookup"))
    app.add_url_rule("/item", "item", lambda: "ok", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    with app.test_client() as client:
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            assert client.open("/item", method=method).status_code == 403
        assert calls == []
        assert client.get("/item").status_code == 200


def test_session_gate_preserves_host_bootstrap_and_authenticated_requests(monkeypatch):
    from work_buddy.dashboard import local_identity_api
    from work_buddy.security.local_identity import LocalIdentityError
    app = Flask(__name__)
    app.extensions["dashboard_read_only"] = lambda: False
    app.before_request(read_only.guard_dashboard_request)
    app.add_url_rule("/item", "item", lambda: "ok", methods=["POST"])
    app.add_url_rule("/redeem", "local_identity.redeem_bootstrap", lambda: "redeemed", methods=["POST"])
    def unauthenticated(**kwargs):
        raise LocalIdentityError("session_unavailable", "No session", status=401)
    monkeypatch.setattr(local_identity_api, "authenticate_request_session", unauthenticated)
    with app.test_client() as client:
        assert client.post("/item").status_code == 403
        assert client.post("/redeem").status_code == 200
        monkeypatch.setattr(local_identity_api, "authenticate_request_session", lambda **kwargs: object())
        assert client.post("/item").status_code == 200


def test_host_session_exception_never_allows_read_only_write():
    app = Flask(__name__)
    app.extensions["dashboard_read_only"] = lambda: True
    app.before_request(read_only.guard_dashboard_request)
    app.add_url_rule("/control", "control", lambda: "ok", methods=["POST"])
    read_only.register_session_exception(app, "control", "The host checks a disposable harness nonce.")
    with app.test_client() as client:
        assert client.post("/control").status_code == 403
    with pytest.raises(ValueError):
        read_only.register_session_exception(app, "absent", "A reason")


@pytest.mark.parametrize("port", [None, 0, 5124, 5126, 5127, 65536])
def test_read_only_server_needs_separate_explicit_port(port):
    with pytest.raises(ValueError, match="explicit port"):
        read_only.serve(port=port)


@pytest.mark.parametrize("endpoint", sorted(read_only.HOST_CALLBACK_EXCEPTIONS))
def test_named_host_callbacks_keep_local_compatibility_and_refuse_browser_bypass(endpoint, monkeypatch):
    from work_buddy.dashboard import local_identity_api
    from work_buddy.security.local_identity import LocalIdentityError
    app = Flask(__name__)
    app.extensions["dashboard_read_only"] = lambda: False
    app.before_request(read_only.guard_dashboard_request)
    app.add_url_rule("/callback", endpoint, lambda: "ok", methods=["POST"])
    def missing(**kwargs):
        raise LocalIdentityError("session_unavailable", "No session", status=401)
    monkeypatch.setattr(local_identity_api, "authenticate_request_session", missing)
    with app.test_client() as client:
        assert client.post("/callback").status_code == 200
        for headers in ({"Origin": "http://localhost"}, {"Referer": "http://localhost/"}, {"Sec-Fetch-Site": "same-origin"}, {"X-Forwarded-For": "10.0.0.1"}, {"Tailscale-User-Login": "remote"}, {"Host": "remote.example"}):
            assert client.post("/callback", headers=headers).status_code == 403
        assert client.post("/callback", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 403
        app.extensions["dashboard_read_only"] = lambda: True
        assert client.post("/callback").status_code == (200 if endpoint in read_only.READ_ONLY_EXCEPTIONS else 403)


def test_safe_method_authentication_does_not_touch_identity(monkeypatch):
    from work_buddy.dashboard.local_identity_api import authenticate_request_session
    from work_buddy.security.local_identity import SESSION_COOKIE_NAME
    app = Flask(__name__)
    observed = []
    class Authority:
        def authenticate_session(self, **kwargs):
            observed.append(kwargs)
            return object()
    with app.test_request_context("/read", headers={"Cookie": f"{SESSION_COOKIE_NAME}=fixture"}):
        authenticate_request_session(authority=Authority())
    assert observed[0]["touch"] is False


def test_only_bundled_document_kernel_can_start_with_windows_quoting(monkeypatch):
    import shutil
    import subprocess
    node = str(Path("node.exe").resolve())
    monkeypatch.setattr(shutil, "which", lambda _name: node)
    worker = Path(read_only.__file__).resolve().parents[1] / "document_kernel" / "runtime_dist" / "worker.mjs"
    command = [node, str(worker)]
    assert read_only._is_document_kernel(command)
    assert read_only._is_document_kernel(subprocess.list2cmdline(command))
    assert not read_only._is_document_kernel(subprocess.list2cmdline(command + ["--extra"]))
    assert not read_only._is_document_kernel([node, str(worker.with_name("other.mjs"))])


def test_survey_logical_hash_detects_same_count_update_in_wal(tmp_path):
    from scripts.survey_dashboard_read_only import authoritative_state
    connection = sqlite3.connect(tmp_path / "authority.db")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value BLOB)")
        connection.execute("INSERT INTO items VALUES (1, ?)", (b"before",))
        connection.commit()
        before = authoritative_state(tmp_path)["authority.db"]["tables"]["items"]
        connection.execute("UPDATE items SET value=? WHERE id=1", (b"after",))
        connection.commit()
        after = authoritative_state(tmp_path)["authority.db"]["tables"]["items"]
        assert before["rows"] == after["rows"] == 1
        assert before["sha256"] != after["sha256"]
    finally:
        connection.close()


def test_read_only_tool_status_uses_cache_without_probe_or_persistence(tmp_path, monkeypatch):
    from work_buddy import tools
    path = tmp_path / "tool_status.json"
    path.write_text('{"cached": {"available": true, "reason": "persisted observation"}}', encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(storage_posture, "_enabled", True)
    monkeypatch.setattr(tools, "_TOOL_STATUS_FILE", path)
    monkeypatch.setattr(tools, "_TOOL_STATUS", None)
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only status read ran or persisted a probe")
    monkeypatch.setattr(tools, "_persist_tool_status", forbidden)
    monkeypatch.setattr(tools, "_TOOL_PROBES", {
        name: tools.ToolProbe(name, name, forbidden) for name in ("cached", "absent")
    })
    observed = tools.probe_all(force=True)
    assert observed["cached"]["available"] is True
    assert observed["absent"]["available"] is False
    assert "disabled" in observed["absent"]["reason"]
    assert path.read_bytes() == before
