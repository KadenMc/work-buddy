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
    assert "FolderBrowserDialog" in observed[0][-1]


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
