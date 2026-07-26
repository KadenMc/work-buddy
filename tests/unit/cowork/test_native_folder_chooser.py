from __future__ import annotations

import subprocess

import pytest

from work_buddy.cowork import native_folder_chooser


def test_windows_picker_returns_the_exact_selected_host_path(monkeypatch) -> None:
    observed: list[list[str]] = []

    def run(command, **_kwargs):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="C:\\Vaults\\My Folder", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert native_folder_chooser._choose_windows("powershell.exe") == "C:\\Vaults\\My Folder"
    assert "-STA" in observed[0]
    script = observed[0][-1]
    assert "IFileOpenDialog" in script
    assert "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7" in script
    assert "FOS_PICKFOLDERS" in script
    assert "FOS_FORCEFILESYSTEM" in script
    assert "FOS_PATHMUSTEXIST" in script
    assert 'SetTitle("Open Folder")' in script
    assert "GetForegroundWindow" in script
    assert "dialog.Show(owner)" in script
    assert "FolderBrowserDialog" not in script
    assert "System.Windows.Forms" not in script


def test_windows_picker_cancel_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, native_folder_chooser._WINDOWS_CANCELLED, stdout="", stderr=""
        ),
    )

    assert native_folder_chooser._choose_windows("powershell.exe") is None


def test_native_picker_failure_is_typed(monkeypatch) -> None:
    def fail(_command, **_kwargs):
        raise OSError("missing host integration")

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(native_folder_chooser.NativeFolderChooserError):
        native_folder_chooser._choose_windows("powershell.exe")


def test_native_picker_timeout_is_short_and_recoverable(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def run(_command, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired("picker", kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(native_folder_chooser.NativeFolderChooserError):
        native_folder_chooser._choose_windows("powershell.exe")
    assert observed["timeout"] == native_folder_chooser._DIALOG_TIMEOUT_SECONDS == 120


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
