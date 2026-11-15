"""Splash screen shown while WaddleMail starts (loading the web engine and the mail store takes
a moment): the app icon, the name, the version and what it is doing. Colours follow the
system theme."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QSplashScreen

from .. import __version__
from ..desktop import icon_svg

W, H = 440, 260


def _card(status: str) -> QPixmap:
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if \
        QGuiApplication.primaryScreen() else 1.0
    pm = QPixmap(int(W * ratio), int(H * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    pal = QApplication.palette()
    base, text = pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.WindowText)
    dim = pal.color(QPalette.ColorRole.PlaceholderText)
    accent = QColor("#4f8cff")  # the envelope's blue

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    card = QPainterPath()
    card.addRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), 14, 14)
    p.fillPath(card, base)
    edge = QColor(text)
    edge.setAlpha(40)
    p.setPen(edge)
    p.drawPath(card)
    p.save()
    p.setClipPath(card)
    p.fillRect(QRectF(0, H - 4, W, 4), accent)  # thin accent strip along the bottom
    p.restore()

    QSvgRenderer(str(icon_svg())).render(p, QRectF(32, 54, 128, 128))

    title = QFont(QApplication.font())
    title.setPointSizeF(title.pointSizeF() * 2.1)
    title.setBold(True)
    p.setFont(title)
    p.setPen(text)
    p.drawText(QRectF(180, 78, W - 196, 44), Qt.AlignmentFlag.AlignVCenter, "WaddleMail")
    small = QFont(QApplication.font())
    p.setFont(small)
    p.setPen(dim)
    p.drawText(QRectF(182, 122, W - 196, 24), Qt.AlignmentFlag.AlignVCenter,
               "Mail for Microsoft 365")
    p.drawText(QRectF(182, 144, W - 196, 24), Qt.AlignmentFlag.AlignVCenter,
               f"Version {__version__}")
    p.setPen(text)
    p.drawText(QRectF(32, H - 44, W - 64, 24), Qt.AlignmentFlag.AlignVCenter, status)
    p.end()
    return pm


class Splash(QSplashScreen):
    def __init__(self):
        super().__init__(_card("Starting…"))
        # rounded corners: let the desktop show through outside the card
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def status(self, text: str) -> None:
        """Show what is happening now (repainted at once, even while startup blocks)."""
        self.setPixmap(_card(text))
        QApplication.processEvents()
