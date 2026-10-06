"""The sidecar's recorded runtime context: persisted, shown, and checked.

The daemon records the context it actually runs with once at boot. Status
and the health check read that record, so they agree, and both report the
drift that makes console programs open windows.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from work_buddy.sidecar import state as sidecar_state
from work_buddy.sidecar.state import HostRecord, SidecarState


def _record(**overrides) -> HostRecord:
    # Forward slashes, so status parses these paths the same way on the
    # Linux runners as on Windows.
    fields = {
        "role": "sidecar",
        "executable": "C:/wb/.venv/Scripts/python.exe",
        "image": "console",
        "console": "allocated",
        "mechanism": "allocate_api",
        "child_python": "C:/wb/.venv/Scripts/python.exe",
        "child_image": "console",
        "pin": None,
        "pin_outside_project": False,
    }
    fields.update(overrides)
    return HostRecord(**fields)


# -- persistence ----------------------------------------------------------------


def test_the_record_round_trips_through_the_state_file() -> None:
    state = SidecarState(pid=4242, started_at=time.time(), host=_record())

    sidecar_state.save_state(state)
    loaded = sidecar_state.load_state()

    assert loaded is not None
    assert loaded.host == _record()


def test_a_state_file_without_the_record_loads_with_none() -> None:
    sidecar_state.STATE_FILE.write_text(json.dumps({"pid": 4242, "started_at": 1.0}))

    loaded = sidecar_state.load_state()

    assert loaded is not None and loaded.host is None


def test_keys_from_a_newer_daemon_are_ignored() -> None:
    record = HostRecord.from_dict({**_record().__dict__, "added_later": True})

    assert record == _record()


# -- warnings ------------------------------------------------------------------------


def test_a_windowless_context_has_no_warnings() -> None:
    assert _record().warnings() == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"console": "none", "mechanism": "none"}, "no console"),
        ({"child_image": "gui", "child_python": r"C:\wb\pythonw.exe"}, "pythonw.exe"),
        ({"pin": r"D:\other\python.exe", "pin_outside_project": True}, "outside the project"),
    ],
    ids=["no-console", "gui-services", "pin-outside"],
)
def test_each_kind_of_drift_warns(overrides, fragment) -> None:
    [warning] = _record(**overrides).warnings()

    assert fragment in warning


def test_only_window_drift_is_a_window_risk() -> None:
    pinned = _record(pin=r"D:\other\python.exe", pin_outside_project=True)

    assert pinned.window_risks() == []
    assert len(_record(console="none").window_risks()) == 1


# -- status -------------------------------------------------------------------------------


def test_status_prints_the_runtime_line(monkeypatch, capsys) -> None:
    from work_buddy.cli import commands, lifecycle

    state = SidecarState(pid=4242, started_at=time.time(), last_tick_at=time.time(), host=_record())
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 4242, "state": state},
    )

    assert commands.cmd_status(SimpleNamespace(json=False)) == 0

    out, err = capsys.readouterr()
    assert (
        "Runtime: python.exe, console allocated without window | services: "
        "C:/wb/.venv/Scripts/python.exe (sidecar interpreter)"
    ) in out
    assert err == ""


def test_status_warns_on_drift(monkeypatch, capsys) -> None:
    from work_buddy.cli import commands, lifecycle

    host = _record(
        executable="C:/wb/.venv/Scripts/pythonw.exe", image="gui", console="none",
        mechanism="none", pin="C:/wb/.venv/Scripts/python.exe",
    )
    state = SidecarState(pid=4242, started_at=time.time(), last_tick_at=time.time(), host=host)
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 4242, "state": state},
    )

    commands.cmd_status(SimpleNamespace(json=False))

    out, err = capsys.readouterr()
    assert "Runtime: pythonw.exe, NO console" in out
    assert "(pinned)" in out
    assert "Warning: the sidecar has no console" in err


def test_status_json_includes_the_record(monkeypatch, capsys) -> None:
    from work_buddy.cli import commands, lifecycle

    state = SidecarState(pid=4242, started_at=time.time(), last_tick_at=time.time(), host=_record())
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 4242, "state": state},
    )

    commands.cmd_status(SimpleNamespace(json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"]["host"]["mechanism"] == "allocate_api"


def test_status_without_a_record_prints_no_runtime_line(monkeypatch, capsys) -> None:
    from work_buddy.cli import commands, lifecycle

    state = SidecarState(pid=4242, started_at=time.time(), last_tick_at=time.time())
    monkeypatch.setattr(
        lifecycle, "sidecar_status",
        lambda: {"running": True, "health": "up", "pid": 4242, "state": state},
    )

    commands.cmd_status(SimpleNamespace(json=False))

    assert "Runtime:" not in capsys.readouterr().out


# -- health -------------------------------------------------------------------------------------


def test_health_passes_a_windowless_context() -> None:
    from work_buddy.health.checks import check_sidecar_runtime_context

    sidecar_state.save_state(SidecarState(pid=1, host=_record()))

    result = check_sidecar_runtime_context()

    assert result["ok"] is True
    assert "allocated" in result["detail"]


def test_health_fails_a_daemon_without_a_console() -> None:
    from work_buddy.health.checks import check_sidecar_runtime_context

    sidecar_state.save_state(SidecarState(pid=1, host=_record(console="none")))

    result = check_sidecar_runtime_context()

    assert result["ok"] is False
    assert "no console" in result["detail"]


def test_health_reports_but_passes_a_pin_outside_the_project() -> None:
    from work_buddy.health.checks import check_sidecar_runtime_context

    sidecar_state.save_state(SidecarState(
        pid=1, host=_record(pin=r"D:\other\python.exe", pin_outside_project=True),
    ))

    result = check_sidecar_runtime_context()

    assert result["ok"] is True
    assert "outside the project" in result["detail"]


def test_health_passes_when_nothing_is_recorded() -> None:
    from work_buddy.health.checks import check_sidecar_runtime_context

    sidecar_state.save_state(SidecarState(pid=1))

    assert check_sidecar_runtime_context()["ok"] is True
