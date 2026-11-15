"""Drag and drop between the message list and the folder pane: the data format and the image
that follows the pointer."""
from __future__ import annotations

import json

from PySide6.QtCore import QByteArray, QMimeData, QPoint, QRect, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

MESSAGES = "application/x-waddlemail-messages"  # {"folder": source folder id, "ids": [...]}
FOLDER = "application/x-waddlemail-folder"      # {"folder": folder id}


def encode(mime_type: str, payload: dict) -> QMimeData:
    data = QMimeData()
    data.setData(mime_type, QByteArray(json.dumps(payload).encode("utf-8")))
    return data


def decode(mime: QMimeData, mime_type: str) -> dict | None:
    if not mime.hasFormat(mime_type):
        return None
    try:
        return json.loads(bytes(mime.data(mime_type)).decode("utf-8"))
    except ValueError:
        return None


def label_pixmap(text: str) -> tuple[QPixmap, QPoint]:
    """A small rounded label ("3 messages") to drag, and its hot spot."""
    font = QApplication.font()
    fm = QFontMetrics(font)
    w, h = fm.horizontalAdvance(text) + 20, fm.height() + 10
    ratio = QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1
    pm = QPixmap(int(w * ratio), int(h * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    pal = QApplication.palette()
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(pal.color(QPalette.ColorRole.Highlight))
    p.drawRoundedRect(QRect(0, 0, w, h), 6, 6)
    p.setPen(pal.color(QPalette.ColorRole.HighlightedText))
    p.setFont(font)
    p.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm, QPoint(-12, -12)  # offset from the pointer, so the target stays visible
