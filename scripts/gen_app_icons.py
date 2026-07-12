"""Generate launcher and PWA icons from the canonical work-buddy SVG mark.

The outputs are committed assets. Re-run this script when ``docs/logo.svg``
changes::

    uv run --no-sync python scripts/gen_app_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

REPO = Path(__file__).resolve().parent.parent
LOGO = REPO / "docs" / "logo.svg"
PWA_DIR = REPO / "dashboard-react" / "public" / "icons"
ICO = REPO / "docs" / "work-buddy.ico"
BACKGROUND = "#161b22"


def _render(size: int, *, maskable: bool = False) -> QImage:
    canvas = QImage(size, size, QImage.Format_ARGB32)
    canvas.fill(QColor(BACKGROUND) if maskable else Qt.transparent)
    renderer = QSvgRenderer(str(LOGO))
    if not renderer.isValid():
        raise SystemExit(f"could not load logo SVG: {LOGO}")

    # Maskable icons keep the mark inside the central safe zone; ordinary
    # icons use more of the canvas while retaining a little visual breathing room.
    inset = size * (0.20 if maskable else 0.08)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    painter.end()
    return canvas


def main() -> None:
    PWA_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in (
        ("app-192.png", 192, False),
        ("app-512.png", 512, False),
        ("app-1024.png", 1024, False),
        ("app-maskable-512.png", 512, True),
    ):
        target = PWA_DIR / name
        if not _render(size, maskable=maskable).save(str(target), "PNG"):
            raise SystemExit(f"failed to write {target}")
        print(f"wrote {target.relative_to(REPO)}")

    source = Image.open(PWA_DIR / "app-512.png").convert("RGBA")
    source.save(
        ICO,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {ICO.relative_to(REPO)}")


if __name__ == "__main__":
    main()
