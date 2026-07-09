"""Qt smoke test: skipped silently wherever the `tray` extra is absent (CI
runs a plain `uv sync`, so these only execute on a dev machine with
`uv sync --extra tray`). Uses the offscreen platform plugin: no real tray or
display is touched, so this only proves the Qt layer constructs (imports,
icon loading), not that an icon renders in a real notification area (that is
the live verification step)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class TestQtBackendConstructs:
    def test_icons_load_for_every_state(self, qapp):
        from work_buddy.tray import qt as tray_qt

        icons = tray_qt._load_icons()
        assert set(icons) == {"up", "booting", "wedged", "down", "busy"}
        for key, icon in icons.items():
            assert not icon.isNull(), f"icon {key} failed to load"

    def test_module_constants(self):
        from work_buddy.tray import qt as tray_qt

        assert tray_qt._POLL_MS >= 1000  # poll must stay far below the 8s stop grace
        assert tray_qt._ASSET_DIR.is_dir()


class TestPanelConstructs:
    """The panel builds and its contextual primary button tracks health."""

    def _panel(self, qapp):
        from work_buddy.tray import qt as tray_qt

        return tray_qt.TrayPanel(qapp, tray_qt._load_icons()["up"])

    def _status(self, health, **kw):
        from work_buddy.tray.status import TrayStatus

        return TrayStatus(
            health=health, busy=kw.get("busy"),
            services_healthy=kw.get("healthy", 0), services_total=kw.get("total", 0),
            pid=1 if health != "down" else None,
            services=kw.get("services", {}),
        )

    def _primary_text(self, panel):
        # The primary action button is the FIRST button in the action box
        # (now nested inside a holder row with Restart), so descend into holders.
        texts = self._button_texts(panel._action_box)
        return texts[0] if texts else None

    def test_primary_is_stop_when_up(self, qapp):
        panel = self._panel(qapp)
        panel.refresh(self._status("up", healthy=5, total=5,
                                   services={"messaging": "healthy"}))
        assert self._primary_text(panel) == "Stop work-buddy"

    def test_primary_is_start_when_down(self, qapp):
        panel = self._panel(qapp)
        panel.refresh(self._status("down"))
        assert self._primary_text(panel) == "Start work-buddy"

    def test_confirm_strip_toggles(self, qapp):
        panel = self._panel(qapp)
        panel.refresh(self._status("up", healthy=5, total=5))
        panel._begin_confirm("stop")
        assert panel._confirm_for == "stop"
        # a Confirm and a Cancel button now exist in the action area
        texts = self._button_texts(panel._action_box)
        assert "Stop" in texts and "Cancel" in texts
        panel._cancel_confirm()
        assert panel._confirm_for is None

    @staticmethod
    def _button_texts(layout):
        from PySide6.QtWidgets import QPushButton

        out = []
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, QPushButton):
                out.append(w.text())
            elif w is not None and w.layout() is not None:
                inner = w.layout()
                for j in range(inner.count()):
                    iw = inner.itemAt(j).widget()
                    if isinstance(iw, QPushButton):
                        out.append(iw.text())
        return out
