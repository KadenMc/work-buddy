"""Shared fixtures for work-buddy test suite.

Key isolation concerns:
- WORK_BUDDY_SESSION_ID must be set before importing most work_buddy modules
- WORK_BUDDY_DATA_DIR must be isolated before collection-time imports resolve paths
- agent_session._cached_session_dir persists across tests — must be reset
- paths.data_dir must never resolve to the native user data root during ordinary tests
- config.py computes USER_TZ at import time — generally fine, but tests that
  need a different timezone should monkeypatch work_buddy.config.USER_TZ
- messaging models resolve DB path from config — override via cfg param
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FORWARDED_NATIVE_DATA_ROOT = os.environ.get("_WORK_BUDDY_PYTEST_NATIVE_DATA_ROOT")
_FORWARDED_NATIVE_ASSET_ROOT = os.environ.get("_WORK_BUDDY_PYTEST_NATIVE_ASSET_ROOT")
_FORWARDED_NATIVE_TASK_DB = os.environ.get("_WORK_BUDDY_PYTEST_NATIVE_TASK_DB")
_INHERITED_DATA_DIR = os.environ.get("WORK_BUDDY_DATA_DIR")
_INHERITED_CONFIG_DIR = os.environ.get("WORK_BUDDY_CONFIG_DIR")
_INHERITED_ASSET_ROOT = os.environ.get("WORK_BUDDY_ASSET_ROOT")


def _startup_config_section(name: str) -> dict:
    """Read one merged config section before the pytest data override lands."""
    config_dir = (
        Path(_INHERITED_CONFIG_DIR).expanduser()
        if _INHERITED_CONFIG_DIR
        else _REPO_ROOT
    )
    merged: dict = {}
    for filename in ("config.yaml", "config.local.yaml"):
        path = config_dir / filename
        if not path.is_file():
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = loaded.get(name) if isinstance(loaded, dict) else None
        if isinstance(section, dict):
            merged.update(section)
    return merged


def _resolve_startup_root(raw: str | os.PathLike[str], *, base: Path) -> Path:
    candidate = Path(raw).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


def _roots_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""
    return left.is_relative_to(right) or right.is_relative_to(left)


_STARTUP_PATHS = _startup_config_section("paths")
_NATIVE_DATA_ROOT = (
    Path(_FORWARDED_NATIVE_DATA_ROOT).resolve()
    if _FORWARDED_NATIVE_DATA_ROOT
    else (
        _resolve_startup_root(_INHERITED_DATA_DIR, base=_REPO_ROOT)
        if _INHERITED_DATA_DIR
        else _resolve_startup_root(
            _STARTUP_PATHS.get("data_root", "data"), base=_REPO_ROOT
        )
    )
)
_NATIVE_ASSET_ROOT = (
    Path(_FORWARDED_NATIVE_ASSET_ROOT).resolve()
    if _FORWARDED_NATIVE_ASSET_ROOT
    else (
        _resolve_startup_root(_INHERITED_ASSET_ROOT, base=_REPO_ROOT)
        if _INHERITED_ASSET_ROOT
        else _resolve_startup_root(
            _STARTUP_PATHS.get("asset_root") or _REPO_ROOT, base=_REPO_ROOT
        )
    )
)
_STARTUP_TASKS = _startup_config_section("tasks")
_task_db_override = _STARTUP_TASKS.get("db_path")
_NATIVE_TASK_DB = (
    Path(_FORWARDED_NATIVE_TASK_DB).resolve()
    if _FORWARDED_NATIVE_TASK_DB
    else (
        _resolve_startup_root(_task_db_override, base=_REPO_ROOT)
        if _task_db_override
        else _NATIVE_DATA_ROOT / "db" / "task_metadata.db"
    )
)
_STARTUP_PROJECTS = _startup_config_section("projects")
_project_db_override = _STARTUP_PROJECTS.get("db_path")
_NATIVE_PROJECT_DB = (
    _resolve_startup_root(_project_db_override, base=_REPO_ROOT)
    if _project_db_override
    else _NATIVE_DATA_ROOT / "db" / "projects.db"
)
_STARTUP_INDEX = _startup_config_section("index")
_index_db_override = _STARTUP_INDEX.get("db_path")
_NATIVE_INDEX_DB = (
    Path(_index_db_override).expanduser().resolve()
    if _index_db_override
    else _NATIVE_DATA_ROOT / "db" / "index-consolidated.db"
)
_NATIVE_SOURCES_DB = _NATIVE_DATA_ROOT / "db" / "sources" / "store.db"

# xdist workers inherit the controller's already-sandboxed WORK_BUDDY_DATA_DIR.
# Forward the roots captured by the first pytest process explicitly so workers
# do not mistake the controller sandbox for native state.
os.environ["_WORK_BUDDY_PYTEST_NATIVE_DATA_ROOT"] = str(_NATIVE_DATA_ROOT)
os.environ["_WORK_BUDDY_PYTEST_NATIVE_ASSET_ROOT"] = str(_NATIVE_ASSET_ROOT)
os.environ["_WORK_BUDDY_PYTEST_NATIVE_TASK_DB"] = str(_NATIVE_TASK_DB)


# CRITICAL: establish the process data sandbox before ANY work_buddy import.
# Test modules import runtime components during collection, and several of those
# components resolve database/runtime paths into module-level constants. An
# autouse fixture is too late to protect those imports. A process-local temp
# directory also gives every xdist worker (a distinct process) its own root.
# Callers may set WORK_BUDDY_TEST_DATA_DIR to choose the temporary parent, but
# an inherited WORK_BUDDY_DATA_DIR is deliberately ignored: a shell pointed at
# production must not make an ordinary pytest invocation write there.
_test_data_parent_raw = os.environ.get("WORK_BUDDY_TEST_DATA_DIR")
_os_temp_root = Path(tempfile.gettempdir()).resolve()
_test_data_parent = (
    Path(_test_data_parent_raw).expanduser().resolve()
    if _test_data_parent_raw
    else _os_temp_root
)
_protected_native_roots = (
    _NATIVE_DATA_ROOT,
    _NATIVE_ASSET_ROOT,
    _NATIVE_TASK_DB.parent,
    _NATIVE_PROJECT_DB.parent,
    _NATIVE_INDEX_DB.parent,
    _NATIVE_SOURCES_DB.parent,
)
if not _test_data_parent.is_relative_to(_os_temp_root) or any(
    _roots_overlap(_test_data_parent, protected)
    for protected in _protected_native_roots
):
    raise RuntimeError(
        "WORK_BUDDY_TEST_DATA_DIR must be under the OS temporary root and "
        "disjoint from every captured native state root"
    )
if _test_data_parent_raw:
    _test_data_parent.mkdir(parents=True, exist_ok=True)
_TEST_DATA_ROOT = Path(
    tempfile.mkdtemp(
        prefix=f"work-buddy-pytest-{os.getpid()}-",
        dir=_test_data_parent,
    )
).resolve()
if any(
    _roots_overlap(_TEST_DATA_ROOT, protected)
    for protected in _protected_native_roots
):
    shutil.rmtree(_TEST_DATA_ROOT, ignore_errors=True)
    raise RuntimeError("pytest data sandbox overlaps captured native state")
# Register before importing work_buddy/logging. atexit is LIFO, so logging's
# later shutdown hook closes Windows file handles before this best-effort reap.
atexit.register(shutil.rmtree, _TEST_DATA_ROOT, ignore_errors=True)
os.environ["WORK_BUDDY_DATA_DIR"] = str(_TEST_DATA_ROOT)

# CRITICAL: Must be set before ANY work_buddy imports happen during collection.
# Many modules trigger get_logger() -> get_session_dir() at import time.
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-session-00000000")

import pytest


@pytest.fixture
def authenticate_dashboard_client(tmp_path, monkeypatch):
    """Give an opted-in route client a real session from a temporary authority."""
    from work_buddy.dashboard import local_identity_api
    from work_buddy.security.local_identity import LocalIdentityAuthority

    authority = LocalIdentityAuthority(tmp_path / "dashboard-local-identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)

    def authenticate(client):
        bootstrap = authority.mint_bootstrap(origin="http://localhost")
        response = client.post(
            "/api/local-identity/bootstrap/redeem",
            json={"token": bootstrap.token},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200, response.get_json()
        return client

    return authenticate


@pytest.fixture
def declared_thread_action_registry(monkeypatch):
    """Use shipped action schemas without host preferences or registry caches.

    Catalog presentation tests need declared parameters, not live tool probes.
    Resolve the actual declarations against isolated registered ops, then
    restore the caller's operation maps and registry cache after each test.
    """
    from pathlib import Path

    from work_buddy.knowledge.skill_loader import load_declared_skills
    from work_buddy.knowledge.file_store import read_unit
    from work_buddy.knowledge.model import unit_from_dict
    from work_buddy.mcp_server import op_registry, registry

    # Direct op-module imports can leave registrations present before the
    # complete built-in scan. Rebuild against fresh maps so cached module
    # reloads cannot duplicate an earlier test's partial registrations.
    monkeypatch.setattr(op_registry, "_OPS", {})
    monkeypatch.setattr(op_registry, "_OP_EFFECTS", {})
    monkeypatch.setattr(op_registry, "_FAILED_MODULES", set())
    monkeypatch.setattr(op_registry, "_builtins_loaded", False)

    store_dir = Path(__file__).resolve().parents[1] / "knowledge" / "store"
    unit_paths = (
        "journal/journal_append_to_note",
        "threads/thread_dismiss",
        "threads/thread_defer",
        "threads/thread_rename",
    )
    units = {}
    for path in unit_paths:
        raw = read_unit(store_dir, path)
        assert raw is not None, f"Missing action declaration: {path}"
        units[path] = unit_from_dict(path, raw)
    skills, issues = load_declared_skills(units)
    assert not issues, issues
    entries = {entry.name: entry for entry in skills}
    assert set(entries) == {path.rsplit("/", 1)[1] for path in unit_paths}

    def refuse_execution(*_args, **_kwargs):
        pytest.fail("Catalog presentation tests must not execute actions")

    for entry in entries.values():
        entry.callable = refuse_execution
    monkeypatch.setattr(registry, "_REGISTRY", entries)
    return entries


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

    Opt out per-test with ``@pytest.mark.real_task_store`` to exercise the
    canonical resolver inside the process-level test data sandbox, or with
    ``@pytest.mark.real_obsidian_bridge`` to drive the bridge transport against
    the test's own mock/server. Neither marker authorizes native user state;
    the two markers are independent.
    """
    if request.node.get_closest_marker("real_task_store") is None:
        try:
            import work_buddy.obsidian.tasks.store as task_store
            import work_buddy.tasks.store as native_task_store
        except Exception:  # pragma: no cover - defensive
            pass
        else:
            monkeypatch.setattr(
                task_store, "_db_path", lambda: tmp_path / "task_metadata.db",
            )
            monkeypatch.setattr(
                native_task_store,
                "default_task_db_path",
                lambda: tmp_path / "task_metadata.db",
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


@pytest.fixture(scope="session")
def native_state_roots():
    """Startup/native roots captured before conftest installs its data sandbox."""
    return {
        "data": _NATIVE_DATA_ROOT,
        "task_db": _NATIVE_TASK_DB,
        "asset": _NATIVE_ASSET_ROOT,
        "system_knowledge": _NATIVE_ASSET_ROOT / "knowledge" / "store",
        "local_knowledge": _NATIVE_ASSET_ROOT / "knowledge" / "store.local",
    }


@pytest.fixture
def _preserve_native_cutover_authorities(monkeypatch):
    """Keep rehearsal safety checks pointed at native, not pytest, authority.

    The production guard intentionally rejects a configured live root inside
    the OS temporary directory. Ordinary tests put WORK_BUDDY_DATA_DIR there,
    so rehearsal tests use this explicit boundary while the dedicated
    cutover-maintenance guard suite continues to exercise the real resolver.
    """
    import work_buddy.cutover_maintenance as maintenance
    from work_buddy.paths import RESOURCES

    live_paths = tuple(
        (_NATIVE_DATA_ROOT / relative).resolve()
        for name, relative in RESOURCES.items()
        if name.startswith("db/") and str(relative).endswith(".db")
    )
    live_paths = tuple(
        dict.fromkeys(
            (
                *live_paths,
                _NATIVE_TASK_DB,
                _NATIVE_PROJECT_DB,
                _NATIVE_INDEX_DB,
                _NATIVE_SOURCES_DB,
            )
        )
    )
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: (_NATIVE_DATA_ROOT, live_paths),
    )


@pytest.fixture(autouse=True)
def _isolate_journal_authority_fence(tmp_path, monkeypatch):
    """Keep legacy-writer tests from consulting the live Journal seal.

    The production file boundary now checks the durable Journal authority on
    every attempted daily-file write. Characterization tests for legacy file
    helpers must therefore default to a missing, isolated control database;
    tests of the fence itself pass an explicit temporary database path.
    """

    import work_buddy.journal_capture.authority as authority
    from contextlib import contextmanager

    original = authority.existing_authority_mode
    original_guard = authority.legacy_markdown_write_guard
    isolated = tmp_path / "journal_authority_fence.db"

    def _existing(path=None):
        return original(isolated if path is None else path)

    @contextmanager
    def _guard(path=None):
        with original_guard(isolated if path is None else path):
            yield

    monkeypatch.setattr(authority, "existing_authority_mode", _existing)
    monkeypatch.setattr(authority, "legacy_markdown_write_guard", _guard)


@pytest.fixture(autouse=True)
def _isolate_sidecar_runtime_files(tmp_path, monkeypatch, request):
    """Redirect the sidecar PID, instance-lock and state files to temp paths.

    ``work_buddy.sidecar.pid.PID_FILE``,
    ``work_buddy.sidecar.instance_lock.LOCK_FILE`` and
    ``work_buddy.sidecar.state.STATE_FILE`` resolve to the real
    ``.data/runtime/`` files at import time. Tests that exercise the
    write/cleanup/check helpers against those module globals would
    otherwise clobber a live sidecar's runtime files on the dev machine.
    The pid file is the destructive case: it is written once at daemon
    boot and never re-created, so a test deleting it leaves ``wbuddy
    status`` reporting a healthy daemon as not running until the next
    restart. (The state file self-heals on the next supervisor tick.)

    The instance lock needs the redirection for a different reason: a test
    that acquired the REAL lock would be told "another sidecar is running"
    whenever one is, and would report the live daemon's owner metadata as
    its own. Worse, a test that acquired it successfully would hold the
    host's single-instance lock for the duration of the suite, so a sidecar
    starting mid-run would correctly refuse to boot.

    The helpers read the module globals at call time, so patching the
    attributes covers them; tests must reference the patched values via
    the modules, not by-value imports. Opt out per-test with
    ``@pytest.mark.real_sidecar_runtime_files``.
    """
    if request.node.get_closest_marker("real_sidecar_runtime_files") is not None:
        yield
        return
    try:
        import work_buddy.sidecar.instance_lock as lock_mod
        import work_buddy.sidecar.pid as pid_mod
        import work_buddy.sidecar.state as state_mod
    except Exception:  # pragma: no cover - defensive
        yield
        return
    monkeypatch.setattr(pid_mod, "PID_FILE", tmp_path / "sidecar.pid")
    monkeypatch.setattr(lock_mod, "LOCK_FILE", tmp_path / "sidecar.lock")
    monkeypatch.setattr(
        state_mod, "STATE_FILE", tmp_path / "sidecar_state.json",
    )
    # A leaked ``_held`` from an earlier test would make ``is_locked()`` report
    # True for every later test in the process, and its fd would outlive the
    # temp directory it points into.
    monkeypatch.setattr(lock_mod, "_held", None)
    yield
    lock_mod.release()


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
    skills emit fire-and-forget notifications as a side effect
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
