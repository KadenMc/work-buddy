from __future__ import annotations

import json
import subprocess
import sys

import pytest

from work_buddy.cowork import native_folder_chooser
from work_buddy.cowork.folder_picker_helper import (
    PICKER_CANCELLED,
    PICKER_MODE_FOLDER,
    PICKER_MODE_LOCATION,
    PICKER_MODE_MARKDOWN,
    PICKER_PROTOCOL,
)


def _selected(path: str, *, mode: str = PICKER_MODE_FOLDER) -> str:
    return json.dumps({"protocol": PICKER_PROTOCOL, "mode": mode, "path": path})


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


def test_windows_json_protocol_accepts_one_transport_line_terminator(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=_selected("C:\\Vaults\\My Folder") + "\r\n",
            stderr="",
        ),
    )

    assert native_folder_chooser._choose_windows() == "C:\\Vaults\\My Folder"


@pytest.mark.parametrize(
    ("mode", "selection"),
    [
        (PICKER_MODE_MARKDOWN, "notes.md"),
        (PICKER_MODE_LOCATION, "drafts"),
    ],
)
def test_windows_scoped_pickers_pass_only_validated_bounded_arguments(
    monkeypatch,
    tmp_path,
    mode: str,
    selection: str,
) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []
    target = tmp_path / selection

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_selected(str(target), mode=mode),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert native_folder_chooser._choose_windows(
        mode=mode,
        start_directory=tmp_path,
    ) == str(target)
    command, kwargs = observed[0]
    assert command == [
        sys.executable,
        "-I",
        "-m",
        "work_buddy.cowork.folder_picker_helper",
        "--mode",
        mode,
        "--start",
        str(tmp_path.resolve()),
    ]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert not any("powershell" in part.lower() for part in command)


def test_windows_scoped_picker_rejects_invalid_start_before_spawn(
    monkeypatch,
) -> None:
    invoked = False

    def run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows(
            mode=PICKER_MODE_MARKDOWN,
            start_directory="relative",
        )

    assert invoked is False
    assert raised.value.code == "folder_chooser_failed"
    assert raised.value.retryable is False


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
            json.dumps(
                {
                    "protocol": "unknown",
                    "mode": PICKER_MODE_FOLDER,
                    "path": "C:\\Folder",
                }
            ),
        ),
        (
            "wrong-mode",
            json.dumps(
                {
                    "protocol": PICKER_PROTOCOL,
                    "mode": PICKER_MODE_MARKDOWN,
                    "path": "C:\\Folder",
                }
            ),
        ),
        (
            "empty-path",
            json.dumps(
                {
                    "protocol": PICKER_PROTOCOL,
                    "mode": PICKER_MODE_FOLDER,
                    "path": "",
                }
            ),
        ),
        (
            "nul-path",
            json.dumps(
                {
                    "protocol": PICKER_PROTOCOL,
                    "mode": PICKER_MODE_FOLDER,
                    "path": "C:\\Bad\u0000Folder",
                }
            ),
        ),
        (
            "oversized-path",
            json.dumps(
                {
                    "protocol": PICKER_PROTOCOL,
                    "mode": PICKER_MODE_FOLDER,
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
        "wrong-mode",
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


@pytest.mark.parametrize("picker", ["macos", "zenity"])
def test_posix_picker_transport_preserves_selected_path_whitespace(
    monkeypatch,
    picker: str,
) -> None:
    selected = "/tmp/selected folder \t"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=selected + "\r\n",
            stderr="",
        ),
    )

    if picker == "macos":
        result = native_folder_chooser._choose_macos()
    else:
        result = native_folder_chooser._choose_zenity("zenity")

    assert result == selected


@pytest.mark.parametrize("picker", ["macos", "zenity"])
def test_posix_picker_transport_rejects_multiline_output(
    monkeypatch,
    picker: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="/tmp/first\n/tmp/second\n",
            stderr="",
        ),
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        if picker == "macos":
            native_folder_chooser._choose_macos()
        else:
            native_folder_chooser._choose_zenity("zenity")

    assert raised.value.retryable is False
    assert raised.value.diagnostic == "Native picker output contained multiple lines."


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
    observed: list[tuple[list[str], int | None]] = []

    def run(command, *, cancelled_code=1, **_kwargs):
        observed.append((command, cancelled_code))
        if command[0] == "/usr/bin/osascript":
            return native_folder_chooser._macos_cancel_marker(PICKER_MODE_FOLDER)
        return None

    monkeypatch.setattr(native_folder_chooser, "_run_dialog", run)

    assert native_folder_chooser._choose_macos() is None
    assert native_folder_chooser._choose_zenity("zenity") is None

    macos_command, macos_cancelled_code = observed[0]
    zenity_command, zenity_cancelled_code = observed[1]
    assert '"Open Folder"' in macos_command[-1]
    assert "Choose a Folder for Co-work" not in macos_command[-1]
    assert macos_cancelled_code is None
    assert "--title=Open Folder" in zenity_command
    assert zenity_cancelled_code == 1


@pytest.mark.parametrize(
    "mode",
    [PICKER_MODE_FOLDER, PICKER_MODE_MARKDOWN, PICKER_MODE_LOCATION],
)
def test_macos_cancel_uses_a_mode_bound_success_payload(
    monkeypatch,
    tmp_path,
    mode: str,
) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []
    marker = native_folder_chooser._macos_cancel_marker(mode)

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=marker,
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    call_kwargs = (
        {}
        if mode == PICKER_MODE_FOLDER
        else {"mode": mode, "start_directory": tmp_path}
    )
    assert native_folder_chooser._choose_macos(**call_kwargs) is None

    command, kwargs = observed[0]
    script = command[2]
    assert f'return "{marker}"' in script
    assert "error number 2" not in script
    assert kwargs["shell"] is False


def test_macos_cancel_marker_for_another_mode_is_not_cancel(
    monkeypatch,
) -> None:
    wrong_marker = native_folder_chooser._macos_cancel_marker(
        PICKER_MODE_MARKDOWN
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=wrong_marker,
            stderr="",
        ),
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_macos()

    assert raised.value.retryable is False
    assert "invalid protocol payload" in raised.value.diagnostic


@pytest.mark.parametrize(
    "mode",
    [PICKER_MODE_FOLDER, PICKER_MODE_MARKDOWN, PICKER_MODE_LOCATION],
)
def test_macos_nonzero_exit_is_never_misclassified_as_cancel(
    monkeypatch,
    tmp_path,
    mode: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="execution error: picker failed (-1728)",
        ),
    )

    call_kwargs = (
        {}
        if mode == PICKER_MODE_FOLDER
        else {"mode": mode, "start_directory": tmp_path}
    )
    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_macos(**call_kwargs)

    assert raised.value.retryable is True
    assert "status 1" in raised.value.diagnostic
    assert "-1728" in raised.value.diagnostic


def test_scoped_picker_modes_share_the_process_lock(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(native_folder_chooser.os, "name", "nt")
    monkeypatch.setattr(
        native_folder_chooser.importlib.util,
        "find_spec",
        lambda _name: object(),
    )
    monkeypatch.setattr(
        native_folder_chooser,
        "_choose_windows",
        lambda **_kwargs: str(tmp_path / "notes.md"),
    )
    chooser = native_folder_chooser.default_host_markdown_chooser()
    assert chooser is not None

    assert native_folder_chooser._DIALOG_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
            chooser(tmp_path)
    finally:
        native_folder_chooser._DIALOG_LOCK.release()

    assert raised.value.code == "folder_chooser_busy"
    assert raised.value.status == 409
