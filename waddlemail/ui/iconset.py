"""Bundled Lucide icons (ISC licence, see icons/LICENSE-lucide.txt).

The SVGs draw with `currentColor`, which Qt does not resolve, so each icon is tinted to the
current palette's text colour (dimmed when disabled, highlighted-text colour when selected). That keeps them readable in
light and dark themes. Icons are tinted when loaded, so a theme switch needs an app restart.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ICON_DIR = Path(__file__).parent / "icons"
_SIZES = (16, 20, 24, 32, 48, 64)


def tinted_svg(svg: str, color: str) -> str:
    return svg.replace("currentColor", color)


def _pixmap(svg: str, color: QColor, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(tinted_svg(svg, color.name()).encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pm


@cache  # tinted once per run; the folder pane asks for the same few icons on every refresh
def load(name: str) -> QIcon:
    """Return the named icon, or an empty QIcon (button shows text only) if it is missing."""
    path = ICON_DIR / f"{name}.svg"
    if not name or not path.exists():
        return QIcon()
    svg = path.read_text(encoding="utf-8")
    pal = QApplication.palette()
    normal = pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    dimmed = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    selected = pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText)
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(_pixmap(svg, normal, size), QIcon.Mode.Normal)
        icon.addPixmap(_pixmap(svg, dimmed, size), QIcon.Mode.Disabled)
        icon.addPixmap(_pixmap(svg, selected, size), QIcon.Mode.Selected)
    return icon


@cache
def load_colored(name: str, color: str, stroke: str | None = None) -> QIcon:
    """An icon in a fixed colour (`color` like "#d64545") rather than the text colour;
    `stroke` (such as "3") draws it with a heavier line than the usual 2."""
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        return QIcon()
    svg = path.read_text(encoding="utf-8")
    if stroke:
        svg = svg.replace('stroke-width="2"', f'stroke-width="{stroke}"')
    selected = QApplication.palette().color(QPalette.ColorGroup.Active,
                                            QPalette.ColorRole.HighlightedText)
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(_pixmap(svg, QColor(color), size), QIcon.Mode.Normal)
        icon.addPixmap(_pixmap(svg, selected, size), QIcon.Mode.Selected)  # readable on blue
    return icon


def load_svg_file(path, color: QColor | None = None) -> QIcon:
    """An icon from any SVG file, its `currentColor` parts tinted (default: the text colour)."""
    svg = Path(path).read_text(encoding="utf-8")
    color = color or QApplication.palette().color(QPalette.ColorGroup.Active,
                                                  QPalette.ColorRole.WindowText)
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(_pixmap(svg, color, size), QIcon.Mode.Normal)
    return icon
