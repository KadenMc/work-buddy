from __future__ import annotations

import json
import subprocess
import sys

import pytest

from work_buddy.cowork import native_folder_chooser
from work_buddy.cowork.folder_picker_helper import (
    PICKER_CANCELLED,
    PICKER_PROTOCOL,
)


def _selected(path: str) -> str:
    return json.dumps({"protocol": PICKER_PROTOCOL, "path": path})


def test_windows_picker_uses_the_fixed_python_helper_protocol(monkeypatch) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_selected("C:\\Vaults\\My Folder"),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert native_folder_chooser._choose_windows() == "C:\\Vaults\\My Folder"
    command, kwargs = observed[0]
    assert command == [
        sys.executable,
        "-I",
        "-m",
        "work_buddy.cowork.folder_picker_helper",
    ]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == native_folder_chooser._DIALOG_TIMEOUT_SECONDS
    assert not any("powershell" in part.lower() for part in command)


def test_windows_picker_cancel_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            PICKER_CANCELLED,
            stdout="",
            stderr="",
        ),
    )

    assert native_folder_chooser._choose_windows() is None


@pytest.mark.parametrize(
    ("case", "stdout"),
    [
        ("empty", ""),
        ("not-json", "not-json"),
        (
            "wrong-protocol",
            json.dumps({"protocol": "unknown", "path": "C:\\Folder"}),
        ),
        ("empty-path", json.dumps({"protocol": PICKER_PROTOCOL, "path": ""})),
        (
            "nul-path",
            json.dumps({"protocol": PICKER_PROTOCOL, "path": "C:\\Bad\u0000Folder"}),
        ),
        (
            "oversized-path",
            json.dumps(
                {
                    "protocol": PICKER_PROTOCOL,
                    "path": "C:\\"
                    + ("a" * native_folder_chooser._MAX_SELECTED_PATH_CHARS),
                }
            ),
        ),
        (
            "oversized-output",
            "x" * (native_folder_chooser._MAX_HELPER_OUTPUT_CHARS + 1),
        ),
    ],
    ids=[
        "empty",
        "not-json",
        "wrong-protocol",
        "empty-path",
        "nul-path",
        "oversized-path",
        "oversized-output",
    ],
)
def test_windows_picker_rejects_invalid_helper_protocol(
    monkeypatch,
    case: str,
    stdout: str,
) -> None:
    del case
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        ),
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert raised.value.code == "folder_chooser_failed"
    assert raised.value.status == 503
    assert raised.value.retryable is False


def test_native_picker_diagnostics_are_single_line_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            7,
            stdout="",
            stderr=("first line\r\nsecond line\t" + ("x" * 2000)),
        ),
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert "\r" not in raised.value.diagnostic
    assert "\n" not in raised.value.diagnostic
    assert "\t" not in raised.value.diagnostic
    assert len(raised.value.diagnostic) <= 1000


def test_native_picker_failure_is_typed(monkeypatch) -> None:
    def fail(_command, **_kwargs):
        raise OSError("missing host integration")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert raised.value.status == 503
    assert "OSError" in raised.value.diagnostic


def test_native_picker_timeout_is_bounded_and_recoverable(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def run(_command, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired("picker", kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()
    assert observed["timeout"] == native_folder_chooser._DIALOG_TIMEOUT_SECONDS == 120
    assert raised.value.code == "folder_chooser_timeout"
    assert raised.value.status == 504


def test_picker_lock_reports_a_distinct_conflict(monkeypatch) -> None:
    monkeypatch.setattr(native_folder_chooser.os, "name", "nt")
    monkeypatch.setattr(
        native_folder_chooser.importlib.util,
        "find_spec",
        lambda _name: object(),
    )
    monkeypatch.setattr(
        native_folder_chooser,
        "_choose_windows",
        lambda: "C:\\Folder",
    )
    chooser = native_folder_chooser.default_host_folder_chooser()
    assert chooser is not None

    assert native_folder_chooser._DIALOG_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
            chooser()
    finally:
        native_folder_chooser._DIALOG_LOCK.release()

    assert raised.value.code == "folder_chooser_busy"
    assert raised.value.status == 409


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
def test_picker_lock_releases_after_every_terminal_outcome(
    monkeypatch,
    outcome: str,
) -> None:
    monkeypatch.setattr(native_folder_chooser.os, "name", "nt")
    monkeypatch.setattr(
        native_folder_chooser.importlib.util,
        "find_spec",
        lambda _name: object(),
    )

    def choose():
        if outcome == "failure":
            raise native_folder_chooser.NativeFolderChooserError("failed")
        return None

    monkeypatch.setattr(native_folder_chooser, "_choose_windows", choose)
    chooser = native_folder_chooser.default_host_folder_chooser()
    assert chooser is not None

    if outcome == "failure":
        with pytest.raises(native_folder_chooser.NativeFolderChooserError):
            chooser()
    else:
        assert chooser() is None

    assert native_folder_chooser._DIALOG_LOCK.acquire(blocking=False)
    native_folder_chooser._DIALOG_LOCK.release()


def test_other_native_pickers_use_the_same_concise_title(monkeypatch) -> None:
    observed: list[tuple[list[str], int]] = []

    def run(command, *, cancelled_code=1):
        observed.append((command, cancelled_code))
        return None

    monkeypatch.setattr(native_folder_chooser, "_run_dialog", run)

    assert native_folder_chooser._choose_macos() is None
    assert native_folder_chooser._choose_zenity("zenity") is None

    macos_command, macos_cancelled_code = observed[0]
    zenity_command, zenity_cancelled_code = observed[1]
    assert '"Open Folder"' in macos_command[-1]
    assert "Choose a Folder for Co-work" not in macos_command[-1]
    assert macos_cancelled_code == 2
    assert "--title=Open Folder" in zenity_command
    assert zenity_cancelled_code == 1
