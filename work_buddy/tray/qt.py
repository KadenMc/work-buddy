"""The PySide6 tray backend. ALL Qt imports are isolated in this module.

Left-click opens a dashboard-styled popover panel (status + controls); right-
click opens a minimal fallback menu (so Quit is always reachable even if the
panel ever fails to render). The panel and menu both drive the same Qt-free
``actions`` helpers.

Threading model: the QApplication event loop owns the main thread. A QTimer
polls every couple of seconds on the GUI thread (reading the small local state
file); lifecycle actions run on a plain worker thread that NEVER touches Qt and
only flips a pending flag. The next timer tick reads status + the pending flag
and refreshes the UI, so no cross-thread signal plumbing is needed.

The same tick doubles as the shutdown listener: when this process no longer
owns ``runtime/tray.pid`` (``wbuddy tray disable`` withdrew it, or a successor
took over), the app quits, which lets Qt remove the icon cleanly instead of
ghosting it in the notification area.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from work_buddy.logging_config import get_logger
from work_buddy.tray import actions, pidfile
from work_buddy.tray import status as tray_status

logger = get_logger(__name__)

_POLL_MS = 2500
_ASSET_DIR = Path(__file__).parent / "assets"
_ICON_KEYS = ("up", "booting", "wedged", "down", "busy")
_ICON_SIZES = (16, 24, 32, 48)

# Dashboard palette (verbatim from dashboard/frontend/styles.py) so the panel
# reads as one product with the web dashboard.
_C = {
    "bg": "#161b22",
    "bg2": "#0d1117",
    "bg3": "#21262d",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#D87857",
    "green": "#3fb950",
    "yellow": "#d29922",
    "red": "#f85149",
    "blue": "#58a6ff",
}
# icon_key (up/booting/wedged/down/busy) -> status dot color.
_STATE_COLOR = {
    "up": _C["green"],
    "booting": _C["yellow"],
    "wedged": _C["red"],
    "down": _C["muted"],
    "busy": _C["blue"],
}
_HEALTH_LABEL = {
    "up": "Running",
    "booting": "Starting up...",
    "wedged": "Wedged (not responding)",
    "down": "Stopped",
}
# per-service status -> dot color
_SVC_COLOR = {"healthy": _C["green"], "starting": _C["yellow"]}


def _load_icons() -> dict[str, QIcon]:
    icons: dict[str, QIcon] = {}
    for key in _ICON_KEYS:
        icon = QIcon()
        for size in _ICON_SIZES:
            icon.addFile(str(_ASSET_DIR / f"tray-{key}-{size}.png"))
        icons[key] = icon
    return icons


def _dot(color: str, size: int = 10) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


_STYLESHEET = f"""
QWidget#trayPanel {{
    background: {_C['bg']};
    border: 1px solid {_C['border']};
    border-radius: 10px;
}}
QLabel {{ color: {_C['text']}; background: transparent; }}
QLabel#title {{ font-size: 14px; font-weight: 600; }}
QLabel#health {{ font-size: 13px; font-weight: 600; }}
QLabel#sub {{ color: {_C['muted']}; font-size: 11px; }}
QLabel#svcName {{ color: {_C['muted']}; font-size: 12px; }}
QFrame#sep {{ background: {_C['border']}; max-height: 1px; border: none; }}
QPushButton {{
    background: {_C['bg3']};
    color: {_C['text']};
    border: 1px solid {_C['border']};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}}
QPushButton:hover {{ border-color: {_C['muted']}; }}
QPushButton:disabled {{ color: {_C['muted']}; background: {_C['bg2']}; }}
QPushButton#primary {{
    background: {_C['accent']};
    border-color: {_C['accent']};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#danger {{ border-color: {_C['red']}; color: {_C['red']}; }}
"""


class TrayPanel(QWidget):
    """The dashboard-styled popover. Rebuilt state via :meth:`refresh`."""

    def __init__(self, app: QApplication, logo: QIcon) -> None:
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self._app = app
        self.setObjectName("trayPanel")
        self.setStyleSheet(_STYLESHEET)
        self.setFixedWidth(380)

        self._worker: threading.Thread | None = None
        self._pending_label: str | None = None
        self._confirm_for: str | None = None  # "stop" | "restart" while confirming

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # Header: logo + title + status dot
        header = QHBoxLayout()
        logo_lbl = QLabel()
        logo_lbl.setPixmap(logo.pixmap(20, 20))
        header.addWidget(logo_lbl)
        title = QLabel("work-buddy")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self._dot_lbl = QLabel()
        header.addWidget(self._dot_lbl)
        root.addLayout(header)

        self._health_lbl = QLabel("...")
        self._health_lbl.setObjectName("health")
        root.addWidget(self._health_lbl)
        self._sub_lbl = QLabel("")
        self._sub_lbl.setObjectName("sub")
        self._sub_lbl.setWordWrap(True)
        root.addWidget(self._sub_lbl)

        root.addWidget(self._sep())

        # Per-service list (rebuilt each refresh), two columns to stay wide-not-tall.
        self._svc_box = QGridLayout()
        self._svc_box.setHorizontalSpacing(16)
        self._svc_box.setVerticalSpacing(4)
        root.addLayout(self._svc_box)

        # Action area (primary + restart, or the confirm strip)
        self._action_box = QVBoxLayout()
        self._action_box.setSpacing(6)
        root.addWidget(self._sep())
        root.addLayout(self._action_box)

        # Footer: open dashboard / activity / quit
        root.addWidget(self._sep())
        footer = QHBoxLayout()
        footer.setSpacing(6)
        btn_dash = QPushButton("Open dashboard")
        btn_dash.clicked.connect(lambda: self._open(""))
        footer.addWidget(btn_dash)
        btn_act = QPushButton("Activity")
        btn_act.setToolTip("Open the event log (Settings -> Activity)")
        btn_act.clicked.connect(lambda: self._open(actions.ACTIVITY_HASH))
        footer.addWidget(btn_act)
        root.addLayout(footer)
        btn_quit = QPushButton("Quit tray")
        btn_quit.setToolTip("Close the tray icon only; the sidecar keeps running")
        btn_quit.clicked.connect(self._app.quit)
        root.addWidget(btn_quit)

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setObjectName("sep")
        f.setFrameShape(QFrame.HLine)
        return f

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # -- rendering --------------------------------------------------------

    def refresh(self, st: tray_status.TrayStatus) -> None:
        pending = self._worker is not None and self._worker.is_alive()
        if not pending:
            self._pending_label = None

        key = tray_status.icon_key(st)
        self._dot_lbl.setPixmap(_dot(_STATE_COLOR.get(key, _C["muted"]), 12))
        self._health_lbl.setText(
            self._pending_label or _HEALTH_LABEL.get(st.health, st.health)
        )
        # sub line: busy job, or a hint for wedged
        sub = ""
        if st.busy:
            job = st.busy.get("job")
            sub = f"busy on job '{job}'" if job else f"busy in {st.busy.get('phase')}"
        elif st.health == "wedged":
            sub = "Use Restart to recover."
        self._sub_lbl.setText(sub)
        self._sub_lbl.setVisible(bool(sub))

        self._render_services(st)
        if self._confirm_for is None:
            self._render_actions(st, pending)

    def _render_services(self, st: tray_status.TrayStatus) -> None:
        self._clear(self._svc_box)
        if not st.services:
            return
        for i, (name, status) in enumerate(st.services.items()):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            dot = QLabel()
            dot.setPixmap(_dot(_SVC_COLOR.get(status, _C["red"]), 8))
            row.addWidget(dot)
            lbl = QLabel(name)
            lbl.setObjectName("svcName")
            row.addWidget(lbl)
            row.addStretch(1)
            holder = QWidget()
            holder.setLayout(row)
            self._svc_box.addWidget(holder, i // 2, i % 2)

    def _render_actions(self, st: tray_status.TrayStatus, pending: bool) -> None:
        self._clear(self._action_box)
        enabled = tray_status.menu_enabled(st.health, pending)
        # Primary: Start when down/wedged, Stop when up; disabled while booting.
        if st.health == "up":
            primary = QPushButton("Stop work-buddy")
            primary.setObjectName("danger")
            primary.setEnabled(enabled["stop"])
            primary.clicked.connect(lambda: self._begin_confirm("stop"))
        else:
            primary = QPushButton("Start work-buddy")
            primary.setObjectName("primary")
            primary.setEnabled(enabled["start"])
            primary.clicked.connect(lambda: self._run("Starting", actions.start_sidecar))
        restart = QPushButton("Restart")
        restart.setEnabled(enabled["restart"])
        restart.clicked.connect(lambda: self._begin_confirm("restart"))

        # Primary (hero) + Restart share one row so the panel reads wide, not tall.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(primary, 2)
        row.addWidget(restart, 1)
        holder = QWidget()
        holder.setLayout(row)
        self._action_box.addWidget(holder)

    def _begin_confirm(self, which: str) -> None:
        """Swap the action row for a confirm strip (Stop/Restart are destructive:
        they kill the sidecar, including the MCP gateway an agent talks through).
        """
        self._confirm_for = which
        self._clear(self._action_box)
        word = "Stop" if which == "stop" else "Restart"
        prompt = QLabel(f"{word} work-buddy? This interrupts running work.")
        prompt.setObjectName("sub")
        prompt.setWordWrap(True)
        self._action_box.addWidget(prompt)
        row = QHBoxLayout()
        row.setSpacing(6)
        confirm = QPushButton(word)
        confirm.setObjectName("danger")
        fn = actions.stop_sidecar if which == "stop" else actions.restart_sidecar
        confirm.clicked.connect(lambda: self._run(f"{word}ping", fn))
        row.addWidget(confirm)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel_confirm)
        row.addWidget(cancel)
        holder = QWidget()
        holder.setLayout(row)
        self._action_box.addWidget(holder)

    def _cancel_confirm(self) -> None:
        self._confirm_for = None
        self.refresh(tray_status.read_status())

    def _run(self, gerund: str, fn) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._confirm_for = None
        self._pending_label = f"{gerund}..."
        self._health_lbl.setText(self._pending_label)
        self._clear(self._action_box)
        note = QLabel(f"{gerund}, please wait...")
        note.setObjectName("sub")
        self._action_box.addWidget(note)

        def _work() -> None:
            try:
                res = fn()
                logger.info("tray action %s: %s", gerund, res.get("detail", res))
            except Exception:
                logger.exception("tray action %s failed", gerund)

        self._worker = threading.Thread(target=_work, daemon=True, name=f"tray-{gerund}")
        self._worker.start()

    def _open(self, target_hash: str) -> None:
        try:
            actions.open_dashboard(target_hash)
        except Exception:
            logger.exception("open dashboard failed")
        self.hide()


class TrayApp:
    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._icons = _load_icons()
        self._panel = TrayPanel(app, self._icons["up"])

        # Minimal right-click fallback menu.
        self._menu = QMenu()
        self._m_open = self._menu.addAction("Open dashboard")
        self._m_open.triggered.connect(lambda: self._panel._open(""))
        self._menu.addSeparator()
        self._m_start = self._menu.addAction("Start")
        self._m_start.triggered.connect(lambda: self._panel._run("Starting", actions.start_sidecar))
        self._m_stop = self._menu.addAction("Stop")
        self._m_stop.triggered.connect(lambda: self._panel._run("Stopping", actions.stop_sidecar))
        self._m_restart = self._menu.addAction("Restart")
        self._m_restart.triggered.connect(lambda: self._panel._run("Restarting", actions.restart_sidecar))
        self._menu.addSeparator()
        self._menu.addAction("Quit tray").triggered.connect(self._app.quit)

        self._tray = QSystemTrayIcon()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._refresh()
        self._tray.show()

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_POLL_MS)

    def _on_activated(self, reason) -> None:
        # Left-click / double-click -> panel; right-click uses the context menu.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_panel()

    def _show_panel(self) -> None:
        self._panel.refresh(tray_status.read_status())
        self._panel.adjustSize()
        w, h = self._panel.width(), self._panel.height()
        screen = self._app.primaryScreen().availableGeometry()
        geo = self._tray.geometry()
        if geo.isValid() and not geo.isEmpty():
            x, y = geo.right() - w, geo.top() - h - 8
        else:
            c = QCursor.pos()
            x, y = c.x() - w, c.y() - h - 8
        x = max(screen.left(), min(x, screen.right() - w))
        y = max(screen.top(), min(y, screen.bottom() - h))
        self._panel.move(x, y)
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()

    def _refresh(self) -> None:
        if not pidfile.owns_pid_file():
            logger.info("Tray pid file withdrawn - quitting.")
            self._app.quit()
            return
        st = tray_status.read_status()
        self._tray.setIcon(self._icons[tray_status.icon_key(st)])
        self._tray.setToolTip(tray_status.tooltip(st))
        enabled = tray_status.menu_enabled(
            st.health, self._panel._worker is not None and self._panel._worker.is_alive()
        )
        self._m_start.setEnabled(enabled["start"])
        self._m_stop.setEnabled(enabled["stop"])
        self._m_restart.setEnabled(enabled["restart"])
        if self._panel.isVisible():
            self._panel.refresh(st)


def run() -> int:
    app = QApplication([])
    app.setApplicationName("work-buddy")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.error("No system tray available on this desktop; exiting.")
        return 1
    tray_app = TrayApp(app)  # noqa: F841 - owns the icon for the app's lifetime
    return app.exec()
