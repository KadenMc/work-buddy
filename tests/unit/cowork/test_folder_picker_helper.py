from __future__ import annotations

import json
import sys
import types

from work_buddy.cowork import folder_picker_helper


def test_helper_emits_one_versioned_selection_with_unicode_safe_json(capsys) -> None:
    selected = "C:\\Projects\\資料 📁"
    result = folder_picker_helper.run_picker(lambda: selected)

    assert result == 0
    output = capsys.readouterr().out
    assert output.isascii()
    assert json.loads(output) == {
        "protocol": folder_picker_helper.PICKER_PROTOCOL,
        "path": selected,
    }


def test_helper_treats_cancel_as_a_normal_distinct_exit(capsys) -> None:
    result = folder_picker_helper.run_picker(lambda: None)

    captured = capsys.readouterr()
    assert result == folder_picker_helper.PICKER_CANCELLED
    assert captured.out == ""
    assert captured.err == ""


def test_helper_contains_failures_on_stderr(capsys) -> None:
    def fail() -> str:
        raise RuntimeError("picker backend unavailable")

    result = folder_picker_helper.run_picker(fail)

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "RuntimeError: picker backend unavailable" in captured.err


def test_qt_helper_requests_the_platform_native_folder_dialog(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakePoint:
        pass

    center = FakePoint()

    class FakeGeometry:
        def center(self):
            return center

    class FakeScreen:
        def availableGeometry(self):
            return FakeGeometry()

    class FakeApplication:
        @staticmethod
        def instance():
            return None

        def __init__(self, argv):
            observed["argv"] = argv

        def setApplicationName(self, value):
            observed["application_name"] = value

        def setOrganizationName(self, value):
            observed["organization_name"] = value

        def primaryScreen(self):
            return FakeScreen()

        def processEvents(self):
            observed["processed_events"] = True

        def quit(self):
            observed["quit"] = True

    class FakeWindowType:
        Tool = object()
        FramelessWindowHint = object()
        WindowStaysOnTopHint = object()

    class FakeQt:
        WindowType = FakeWindowType

    class FakeWidget:
        def __init__(self):
            observed["anchor"] = self
            observed["flags"] = []

        def setWindowFlag(self, flag, enabled):
            observed["flags"].append((flag, enabled))

        def setWindowOpacity(self, opacity):
            observed["opacity"] = opacity

        def resize(self, width, height):
            observed["size"] = (width, height)

        def move(self, point):
            observed["position"] = point

        def show(self):
            observed["shown"] = True

        def raise_(self):
            observed["raised"] = True

        def activateWindow(self):
            observed["activated"] = True

        def close(self):
            observed["closed"] = True

    native_folder_option = object()

    class FakeFileDialog:
        class Option:
            ShowDirsOnly = native_folder_option

        @staticmethod
        def getExistingDirectory(parent, title, start, options):
            observed["dialog"] = (parent, title, start, options)
            return "C:\\Selected"

    pyside = types.ModuleType("PySide6")
    core = types.ModuleType("PySide6.QtCore")
    core.Qt = FakeQt
    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApplication
    widgets.QFileDialog = FakeFileDialog
    widgets.QWidget = FakeWidget
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", core)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)

    assert folder_picker_helper._choose_native_folder() == "C:\\Selected"
    assert observed["dialog"] == (
        observed["anchor"],
        "Open Folder",
        str(folder_picker_helper.Path.home()),
        native_folder_option,
    )
    assert observed["flags"] == [
        (FakeWindowType.Tool, True),
        (FakeWindowType.FramelessWindowHint, True),
        (FakeWindowType.WindowStaysOnTopHint, True),
    ]
    assert observed["opacity"] == 0.01
    assert observed["size"] == (1, 1)
    assert observed["position"] is center
    assert observed["shown"] is True
    assert observed["raised"] is True
    assert observed["activated"] is True
    assert observed["processed_events"] is True
    assert observed["closed"] is True
    assert observed["quit"] is True
