"""Shared fixtures for work-buddy test suite.

Key isolation concerns:
- WORK_BUDDY_SESSION_ID must be set before importing most work_buddy modules
- agent_session._cached_session_dir persists across tests — must be reset
- paths.data_dir resolves to the real data/ — must be redirected for isolation
- config.py computes USER_TZ at import time — generally fine, but tests that
  need a different timezone should monkeypatch work_buddy.config.USER_TZ
- messaging models resolve DB path from config — override via cfg param
"""

import os

# CRITICAL: Must be set before ANY work_buddy imports happen during collection.
# Many modules trigger get_logger() -> get_session_dir() at import time.
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-session-00000000")

import pytest


@pytest.fixture(autouse=True)
def _set_session_env(monkeypatch):
    """Ensure WORK_BUDDY_SESSION_ID is always set for imports."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-session-00000000")


@pytest.fixture(autouse=True)
def _reset_user_tz_cache():
    """Reset every materialized form of ``USER_TZ`` around every test.

    ``work_buddy.config.USER_TZ`` memoizes on first access (`_USER_TZ_CACHE`),
    a boot-time perf optimization. Under pytest that module global leaks across
    tests: whichever test first materializes USER_TZ under a patched or
    timezone-less config poisons the cache for every later test — so the
    timezone-sensitive collectors (`timefmt`, chat/chrome renderers) fail
    order-dependently.

    Some older tests patch the lazy ``USER_TZ`` attribute itself. Pytest then
    restores the value obtained through the module's ``__getattr__`` hook as a
    concrete module attribute, bypassing ``_USER_TZ_CACHE`` on later reads.
    Remove that materialized attribute as well as clearing the backing cache so
    every test resolves the timezone from its own active config.
    """
    import work_buddy.config as _config

    _config.__dict__.pop("USER_TZ", None)
    _config._USER_TZ_CACHE = None
    yield
    _config.__dict__.pop("USER_TZ", None)
    _config._USER_TZ_CACHE = None


@pytest.fixture(autouse=True)
def _isolate_work_item_events(tmp_path, monkeypatch):
    """Redirect the WorkItem base event log to a per-test temp DB.

    Task mutations fire ``_publish_task_event`` which best-effort emits into
    ``work_buddy.threads.work_item_events``. Without this, any test that
    creates/toggles a task would write rows into the real
    ``.data/db/work_item_events.db``. Autouse keeps every test's emission
    isolated. Best-effort import so this never breaks collection.
    """
    try:
        import work_buddy.threads.work_item_events as wie
    except Exception:  # pragma: no cover - defensive
        return
    monkeypatch.setattr(
        wie, "_db_path", lambda: tmp_path / "work_item_events.db",
    )


@pytest.fixture(autouse=True)
def _isolate_task_store_and_vault(tmp_path, monkeypatch, request):
    """Isolate the task metadata store and neutralize the Obsidian bridge.

    Two real resources the per-test sandbox would otherwise leak into:

    * **Task metadata store.** ``work_buddy.obsidian.tasks.store._db_path``
      resolves (via ``paths.resolve("db/tasks")``) to the real
      ``.data/db/task_metadata.db``. Any unmocked ``store.create`` /
      ``mutations.create_task`` would write a real row. Redirected to a
      per-test temp SQLite file.

    * **Obsidian vault.** ``mutations.create_task`` writes the master task
      list through the Obsidian *bridge* — an HTTP PUT to a *running*
      Obsidian, which commits to the real ``tasks/master-task-list.md``.
      The bridge bypasses ``vault_root`` entirely, so redirecting that
      config value would not help. All bridge network I/O funnels through
      ``bridge.urlopen``; replacing it with a connection-refusing stub
      makes every bridge call behave as "Obsidian unreachable", so a
      stray ``create_task`` reads ``None`` and bails via ``bridge_failure``
      *before* it can write to the vault or the store — regardless of
      whether Obsidian is actually running on the dev box.

    Mirrors ``_isolate_work_item_events``. Best-effort imports so a module
    move never breaks collection. The patch only swaps the attribute at
    setup (it never calls ``urlopen`` itself), so tests that re-patch
    ``bridge.urlopen`` / ``bridge._request_with_status`` in their own body
    cleanly override it — e.g. ``test_bridge_typed_exceptions`` and
    ``test_editor_conflict``.

    Opt out per-test with ``@pytest.mark.real_task_store`` (drive the real
    DB path) or ``@pytest.mark.real_obsidian_bridge`` (drive the real
    bridge transport against the test's own mock/server). The two markers
    are independent.
    """
    if request.node.get_closest_marker("real_task_store") is None:
        try:
            import work_buddy.obsidian.tasks.store as task_store
        except Exception:  # pragma: no cover - defensive
            pass
        else:
            monkeypatch.setattr(
                task_store, "_db_path", lambda: tmp_path / "task_metadata.db",
            )

    if request.node.get_closest_marker("real_obsidian_bridge") is None:
        try:
            import work_buddy.obsidian.bridge as bridge
        except Exception:  # pragma: no cover - defensive
            pass
        else:
            def _refuse_bridge_connection(*_args, **_kwargs):
                raise ConnectionRefusedError(
                    "Obsidian bridge disabled in tests "
                    "(_isolate_task_store_and_vault in tests/conftest.py). "
                    "Mock the bridge, or mark @pytest.mark.real_obsidian_bridge "
                    "to opt out."
                )

            monkeypatch.setattr(bridge, "urlopen", _refuse_bridge_connection)


@pytest.fixture(autouse=True)
def _isolate_sidecar_runtime_files(tmp_path, monkeypatch, request):
    """Redirect the sidecar PID and state files to per-test temp paths.

    ``work_buddy.sidecar.pid.PID_FILE`` and
    ``work_buddy.sidecar.state.STATE_FILE`` resolve to the real
    ``.data/runtime/`` files at import time. Tests that exercise the
    write/cleanup/check helpers against those module globals would
    otherwise clobber a live sidecar's runtime files on the dev machine.
    The pid file is the destructive case: it is written once at daemon
    boot and never re-created, so a test deleting it leaves ``wbuddy
    status`` reporting a healthy daemon as not running until the next
    restart. (The state file self-heals on the next supervisor tick.)

    The helpers read the module globals at call time, so patching the
    attributes covers them; tests must reference the patched values via
    the modules, not by-value imports. Opt out per-test with
    ``@pytest.mark.real_sidecar_runtime_files``.
    """
    if request.node.get_closest_marker("real_sidecar_runtime_files") is not None:
        return
    try:
        import work_buddy.sidecar.pid as pid_mod
        import work_buddy.sidecar.state as state_mod
    except Exception:  # pragma: no cover - defensive
        return
    monkeypatch.setattr(pid_mod, "PID_FILE", tmp_path / "sidecar.pid")
    monkeypatch.setattr(
        state_mod, "STATE_FILE", tmp_path / "sidecar_state.json",
    )


@pytest.fixture(autouse=True)
def _isolate_conversation_summary_databases(tmp_path, monkeypatch, request):
    """Keep transcript observation and summarization writes out of live DBs.

    Observing a fixture session can enqueue it as a side effect now that
    summaries are on by default. Both databases therefore need a suite-wide
    boundary; isolating only the observability DB still leaks queue rows into
    the user's real summarization store.
    """
    if request.node.get_closest_marker("real_conversation_databases") is not None:
        return

    import work_buddy.conversation_observability.db as observation_db
    import work_buddy.summarization.db as summarization_db

    observation_path = tmp_path / "conversation_observability.db"
    summarization_path = tmp_path / "summarization.db"
    monkeypatch.setattr(observation_db, "_default_db_path", lambda: observation_path)
    monkeypatch.setattr(
        observation_db, "db_path", lambda cfg=None: observation_db._default_db_path(),
    )
    monkeypatch.setattr(summarization_db, "_default_db_path", lambda: summarization_path)
    monkeypatch.setattr(
        summarization_db, "db_path", lambda cfg=None: summarization_db._default_db_path(),
    )


@pytest.fixture(autouse=True)
def _isolate_notification_delivery(monkeypatch, request):
    """Neutralize outbound notification delivery for every test.

    ``SurfaceDispatcher.deliver`` is the single fan-out point where a
    notification reaches Telegram / Obsidian / dashboard surfaces. Several
    capabilities emit fire-and-forget notifications as a side effect
    (e.g. ``tasks.archive_completed`` -> ``_send_archive_summary_notification``).
    Without this, any unmocked call from a test sends a *real* message (the
    "Archived N completed tasks" Telegram leak). Stubbing ``deliver`` at the
    class level is surface-agnostic and construction-agnostic: it blocks every
    surface regardless of how the dispatcher instance was built, and the
    consequential helpers import the dispatcher lazily inside the function, so
    a class-attribute patch is what intercepts them.

    Mirrors ``_isolate_task_store_and_vault``. Best-effort import so a module
    move never breaks collection. Test-local patches of ``SurfaceDispatcher``
    / ``from_config`` take precedence over this autouse default.

    Opt out per-test with ``@pytest.mark.real_notification_delivery`` to drive
    the real dispatcher against the test's own fakes.
    """
    if request.node.get_closest_marker("real_notification_delivery") is not None:
        return
    try:
        import work_buddy.notifications.dispatcher as disp
    except Exception:  # pragma: no cover - defensive
        return

    def _no_deliver(self, notification, mark_delivered_fn=None):
        return {}  # matches deliver()'s dict[str, bool] contract

    monkeypatch.setattr(disp.SurfaceDispatcher, "deliver", _no_deliver)


@pytest.fixture
def tmp_agents_dir(tmp_path, monkeypatch):
    """Redirect agent_session to write into a temp directory.

    Monkeypatches ``paths.data_dir`` so that ``data_dir("agents")`` returns
    ``tmp_path`` while other categories are unaffected.  Also clears the
    cached session dir so each test starts fresh.

    Returns the temp agents/ directory.
    """
    import work_buddy.agent_session as asmod
    import work_buddy.paths as pmod

    _original_data_dir = pmod.data_dir

    def _patched_data_dir(category: str = "") -> "Path":
        if category == "agents":
            tmp_path.mkdir(parents=True, exist_ok=True)
            return tmp_path
        return _original_data_dir(category)

    monkeypatch.setattr(pmod, "data_dir", _patched_data_dir)
    # Also patch the direct import reference in agent_session
    monkeypatch.setattr(asmod, "data_dir", _patched_data_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)
    return tmp_path


@pytest.fixture
def tmp_messaging_db(tmp_path):
    """Create a fresh in-memory-like SQLite messaging DB in a temp dir.

    Returns (connection, db_path).
    """
    from work_buddy.messaging.models import get_connection

    db_path = tmp_path / "test_messages.db"
    import work_buddy.messaging.models as mmod

    original = mmod._db_path

    def _patched_db_path(c=None):
        return db_path

    mmod._db_path = _patched_db_path
    try:
        conn = get_connection()
        yield conn, db_path
    finally:
        conn.close()
        mmod._db_path = original
