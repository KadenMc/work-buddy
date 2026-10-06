from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from work_buddy.cowork import native_folder_chooser
from work_buddy.cowork.folder_picker_helper import (
    PICKER_CANCELLED,
    PICKER_MODE_FILE,
    PICKER_MODE_FOLDER,
    PICKER_MODE_LOCATION,
    PICKER_MODE_MARKDOWN,
    PICKER_PROTOCOL,
)
from work_buddy.process import ProcessTimeout


def _selected(path: str, *, mode: str = PICKER_MODE_FOLDER) -> str:
    return json.dumps({"protocol": PICKER_PROTOCOL, "mode": mode, "path": path})


def _windows_os_stub() -> SimpleNamespace:
    """Advertise Windows to the module without mutating Python's shared os module."""

    return SimpleNamespace(
        name="nt",
        fspath=native_folder_chooser.os.fspath,
    )


def test_windows_picker_uses_the_fixed_python_helper_protocol(recording_runner) -> None:
    recording_runner.script(stdout=_selected("C:\\Vaults\\My Folder"))

    assert native_folder_chooser._choose_windows() == "C:\\Vaults\\My Folder"
    [launch] = recording_runner.launches
    assert launch.argv == (
        sys.executable,
        "-I",
        "-m",
        "work_buddy.cowork.folder_picker_helper",
    )
    assert launch.popen_kwargs()["shell"] is False
    assert launch.stdin == subprocess.DEVNULL
    assert launch.timeout == native_folder_chooser._DIALOG_TIMEOUT_SECONDS
    assert not any("powershell" in part.lower() for part in launch.argv)


def test_windows_json_protocol_accepts_one_transport_line_terminator(
    recording_runner,
) -> None:
    recording_runner.script(stdout=_selected("C:\\Vaults\\My Folder") + "\r\n")

    assert native_folder_chooser._choose_windows() == "C:\\Vaults\\My Folder"


@pytest.mark.parametrize(
    ("mode", "selection"),
    [
        (PICKER_MODE_FILE, "notes.md"),
        (PICKER_MODE_MARKDOWN, "notes.md"),
        (PICKER_MODE_LOCATION, "drafts"),
    ],
)
def test_windows_scoped_pickers_pass_only_validated_bounded_arguments(
    recording_runner,
    tmp_path,
    mode: str,
    selection: str,
) -> None:
    target = tmp_path / selection
    recording_runner.script(stdout=_selected(str(target), mode=mode))

    assert native_folder_chooser._choose_windows(
        mode=mode,
        start_directory=tmp_path,
    ) == str(target)
    [launch] = recording_runner.launches
    assert launch.argv == (
        sys.executable,
        "-I",
        "-m",
        "work_buddy.cowork.folder_picker_helper",
        "--mode",
        mode,
        "--start",
        str(tmp_path.resolve()),
    )
    assert launch.popen_kwargs()["shell"] is False
    assert launch.stdin == subprocess.DEVNULL
    assert not any("powershell" in part.lower() for part in launch.argv)


def test_windows_scoped_picker_rejects_invalid_start_before_spawn(
    recording_runner,
) -> None:
    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows(
            mode=PICKER_MODE_MARKDOWN,
            start_directory="relative",
        )

    assert recording_runner.launches == []
    assert raised.value.code == "folder_chooser_failed"
    assert raised.value.retryable is False


def test_windows_picker_cancel_is_not_an_error(recording_runner) -> None:
    recording_runner.script(returncode=PICKER_CANCELLED, stdout="", stderr="")

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
    recording_runner,
    case: str,
    stdout: str,
) -> None:
    del case
    recording_runner.script(stdout=stdout, stderr="")

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert raised.value.code == "folder_chooser_failed"
    assert raised.value.status == 503
    assert raised.value.retryable is False


def test_native_picker_diagnostics_are_single_line_and_bounded(recording_runner) -> None:
    recording_runner.script(
        returncode=7,
        stdout="",
        stderr=("first line\r\nsecond line\t" + ("x" * 2000)),
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert "\r" not in raised.value.diagnostic
    assert "\n" not in raised.value.diagnostic
    assert "\t" not in raised.value.diagnostic
    assert len(raised.value.diagnostic) <= 1000


@pytest.mark.parametrize("picker", ["macos", "zenity"])
def test_posix_picker_transport_preserves_selected_path_whitespace(
    recording_runner,
    picker: str,
) -> None:
    selected = "/tmp/selected folder \t"
    recording_runner.script(stdout=selected + "\r\n", stderr="")

    if picker == "macos":
        result = native_folder_chooser._choose_macos()
    else:
        result = native_folder_chooser._choose_zenity("zenity")

    assert result == selected


@pytest.mark.parametrize("picker", ["macos", "zenity"])
def test_posix_picker_transport_rejects_multiline_output(
    recording_runner,
    picker: str,
) -> None:
    recording_runner.script(stdout="/tmp/first\n/tmp/second\n", stderr="")

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        if picker == "macos":
            native_folder_chooser._choose_macos()
        else:
            native_folder_chooser._choose_zenity("zenity")

    assert raised.value.retryable is False
    assert raised.value.diagnostic == "Native picker output contained multiple lines."


def test_native_picker_failure_is_typed(recording_runner) -> None:
    recording_runner.script(raises=OSError("missing host integration"))

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()

    assert raised.value.status == 503
    assert "OSError" in raised.value.diagnostic


def test_native_picker_timeout_is_bounded_and_recoverable(recording_runner) -> None:
    recording_runner.script(
        raises=ProcessTimeout([], native_folder_chooser._DIALOG_TIMEOUT_SECONDS)
    )

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_windows()
    [launch] = recording_runner.launches
    assert launch.timeout == native_folder_chooser._DIALOG_TIMEOUT_SECONDS == 120
    assert raised.value.code == "folder_chooser_timeout"
    assert raised.value.status == 504


def test_picker_lock_reports_a_distinct_conflict(monkeypatch) -> None:
    monkeypatch.setattr(
        native_folder_chooser,
        "os",
        _windows_os_stub(),
    )
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
    monkeypatch.setattr(
        native_folder_chooser,
        "os",
        _windows_os_stub(),
    )
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
    assert '"Open folder"' in macos_command[-1]
    assert "Choose a folder for Co-work" not in macos_command[-1]
    assert macos_cancelled_code is None
    assert "--title=Open folder" in zenity_command
    assert zenity_cancelled_code == 1


def test_native_file_pickers_use_from_file_wording(
    monkeypatch,
    tmp_path,
) -> None:
    observed: list[list[str]] = []

    def run(command, **_kwargs):
        observed.append(command)
        if command[0] == "/usr/bin/osascript":
            return native_folder_chooser._macos_cancel_marker(PICKER_MODE_FILE)
        return None

    monkeypatch.setattr(native_folder_chooser, "_run_dialog", run)

    assert (
        native_folder_chooser._choose_macos(
            mode=PICKER_MODE_FILE,
            start_directory=tmp_path,
        )
        is None
    )
    assert (
        native_folder_chooser._choose_zenity(
            "zenity",
            mode=PICKER_MODE_FILE,
            start_directory=tmp_path,
        )
        is None
    )

    assert '"From file"' in observed[0][2]
    assert "--title=From file" in observed[1]
    assert "--file-filter=Supported files | *.md *.markdown" in observed[1]


def test_legacy_markdown_picker_keeps_a_markdown_only_filter(
    monkeypatch,
    tmp_path,
) -> None:
    observed: list[list[str]] = []

    def run(command, **_kwargs):
        observed.append(command)
        if command[0] == "/usr/bin/osascript":
            return native_folder_chooser._macos_cancel_marker(
                PICKER_MODE_MARKDOWN
            )
        return None

    monkeypatch.setattr(native_folder_chooser, "_run_dialog", run)

    assert (
        native_folder_chooser._choose_macos(
            mode=PICKER_MODE_MARKDOWN,
            start_directory=tmp_path,
        )
        is None
    )
    assert (
        native_folder_chooser._choose_zenity(
            "zenity",
            mode=PICKER_MODE_MARKDOWN,
            start_directory=tmp_path,
        )
        is None
    )

    assert 'of type {"md", "markdown"}' in observed[0][2]
    assert "--file-filter=Markdown files | *.md *.markdown" in observed[1]


@pytest.mark.parametrize(
    "mode",
    [
        PICKER_MODE_FOLDER,
        PICKER_MODE_FILE,
        PICKER_MODE_MARKDOWN,
        PICKER_MODE_LOCATION,
    ],
)
def test_macos_cancel_uses_a_mode_bound_success_payload(
    recording_runner,
    tmp_path,
    mode: str,
) -> None:
    marker = native_folder_chooser._macos_cancel_marker(mode)
    recording_runner.script(stdout=marker, stderr="")

    call_kwargs = (
        {}
        if mode == PICKER_MODE_FOLDER
        else {"mode": mode, "start_directory": tmp_path}
    )
    assert native_folder_chooser._choose_macos(**call_kwargs) is None

    [launch] = recording_runner.launches
    script = launch.argv[2]
    assert f'return "{marker}"' in script
    assert "error number 2" not in script
    assert launch.popen_kwargs()["shell"] is False


def test_macos_cancel_marker_for_another_mode_is_not_cancel(
    recording_runner,
) -> None:
    wrong_marker = native_folder_chooser._macos_cancel_marker(
        PICKER_MODE_MARKDOWN
    )
    recording_runner.script(stdout=wrong_marker, stderr="")

    with pytest.raises(native_folder_chooser.NativeFolderChooserError) as raised:
        native_folder_chooser._choose_macos()

    assert raised.value.retryable is False
    assert "invalid protocol payload" in raised.value.diagnostic


@pytest.mark.parametrize(
    "mode",
    [
        PICKER_MODE_FOLDER,
        PICKER_MODE_FILE,
        PICKER_MODE_MARKDOWN,
        PICKER_MODE_LOCATION,
    ],
)
def test_macos_nonzero_exit_is_never_misclassified_as_cancel(
    recording_runner,
    tmp_path,
    mode: str,
) -> None:
    recording_runner.script(
        returncode=1,
        stdout="",
        stderr="execution error: picker failed (-1728)",
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
    monkeypatch.setattr(
        native_folder_chooser,
        "os",
        _windows_os_stub(),
    )
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


def test_default_import_chooser_uses_generic_file_mode(
    monkeypatch,
    tmp_path,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        native_folder_chooser,
        "os",
        _windows_os_stub(),
    )
    monkeypatch.setattr(
        native_folder_chooser.importlib.util,
        "find_spec",
        lambda _name: object(),
    )

    def choose_windows(**kwargs):
        observed.update(kwargs)
        return str(tmp_path / "notes.md")

    monkeypatch.setattr(
        native_folder_chooser,
        "_choose_windows",
        choose_windows,
    )
    chooser = native_folder_chooser.default_host_import_chooser()
    assert chooser is not None

    assert chooser(tmp_path) == str(tmp_path / "notes.md")
    assert observed == {
        "mode": PICKER_MODE_FILE,
        "start_directory": tmp_path.resolve(),
    }
