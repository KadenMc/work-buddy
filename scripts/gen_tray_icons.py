"""Generate tray icon state glyphs from the real work-buddy mark (docs/logo.svg).

Dev-only one-off. Uses Qt's own SVG renderer (from the ``tray`` extra, already
required to run the tray), so it adds NO system dependency such as cairo. The
committed PNGs are what ship; re-run only to change the design::

    uv run python scripts/gen_tray_icons.py

Design: the brand orange W on a transparent background (reads on light and dark
taskbars alike), overlaid with a status dot whose color encodes the sidecar
state. The dot, not the mark, carries state because a ~16 px glyph reads a
color change far better than a shape change; the mark itself is additionally
dimmed to gray for the ``down`` state so running-vs-stopped is legible at a
glance.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

REPO = Path(__file__).resolve().parent.parent
LOGO = REPO / "docs" / "logo.svg"
ASSET_DIR = REPO / "work_buddy" / "tray" / "assets"

# Colors match dashboard/frontend/styles.py so the tray dot and the panel agree.
STATES = {
    "up": "#3fb950",       # green: healthy
    "booting": "#d29922",  # yellow: starting up
    "wedged": "#f85149",   # red: alive but not responding
    "down": "#8b949e",     # muted: stopped (mark is also dimmed)
    "busy": "#58a6ff",     # blue: healthy but a long dispatch job is running
}
DOWN_MARK = "#8b949e"      # the mark is recolored to this when the sidecar is down
SIZES = (16, 24, 32, 48)
CANVAS = 256               # render large, downscale for anti-aliasing


def _render_mark(painter: QPainter, renderer: QSvgRenderer, dim: bool) -> None:
    # Leave a margin so the corner status dot never swallows the mark.
    mark_rect = QRectF(CANVAS * 0.06, CANVAS * 0.02, CANVAS * 0.82, CANVAS * 0.82)
    renderer.render(painter, mark_rect)
    if dim:
        # Recolor every pixel the mark drew (preserving its alpha) to gray.
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(QRectF(0, 0, CANVAS, CANVAS), QColor(DOWN_MARK))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)


def _draw_status_dot(painter: QPainter, color: str) -> None:
    cx, cy, r = CANVAS * 0.76, CANVAS * 0.76, CANVAS * 0.21
    ring = CANVAS * 0.055  # a transparent-cutout ring separates dot from mark
    painter.setPen(Qt.NoPen)
    painter.setCompositionMode(QPainter.CompositionMode_Clear)
    painter.drawEllipse(
        QRectF(cx - r - ring, cy - r - ring, 2 * (r + ring), 2 * (r + ring))
    )
    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))


def _build(state: str, color: str) -> QImage:
    canvas = QImage(CANVAS, CANVAS, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    renderer = QSvgRenderer(str(LOGO))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    _render_mark(painter, renderer, dim=(state == "down"))
    _draw_status_dot(painter, color)
    painter.end()
    return canvas


def main() -> None:
    if not QSvgRenderer(str(LOGO)).isValid():
        raise SystemExit(f"could not load logo SVG: {LOGO}")
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for state, color in STATES.items():
        canvas = _build(state, color)
        for size in SIZES:
            out = ASSET_DIR / f"tray-{state}-{size}.png"
            scaled = canvas.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            if not scaled.save(str(out), "PNG"):
                raise SystemExit(f"failed to write {out}")
            print(f"wrote {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
