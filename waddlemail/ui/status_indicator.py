"""The status bar's right-hand corner: connection to the server and what WaddleMail is doing.

    ● Up to date · 14:32           green: the server answered the last request
    ● ⟳ Syncing with server…       a job is running (the newest one is named)
    ● Offline · 3 changes waiting  red: the server couldn't be reached; amber: connecting, or
                                   sign-in needed

The connection state comes from the requests themselves (graph.status_hook), reported from
worker threads and handed to the UI thread through a signal. Click the indicator to sync now.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from .. import graph
from . import iconset

CONNECTING = "connecting"
COLORS = {graph.ONLINE: "#2f9e44", graph.OFFLINE: "#e03131", graph.SIGNIN: "#f59f00",
          CONNECTING: "#f59f00"}


class _Dot(QWidget):
    def __init__(self):
        super().__init__()
        self.color = QColor(COLORS[CONNECTING])
        self.setFixedSize(12, 12)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QRectF(2, 2, 8, 8))


class _Spinner(QWidget):
    """The refresh arrow, turning while something is running."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 16)
        self._icon = iconset.load("refresh-cw")
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._turn)
        self.hide()

    def spin(self, on: bool):
        self.setVisible(on)
        if on and not self._timer.isActive():
            self._timer.start()
        elif not on:
            self._timer.stop()

    def _turn(self):
        self._angle = (self._angle + 24) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(QPointF(8, 8))
        p.rotate(self._angle)
        p.drawPixmap(-7, -7, self._icon.pixmap(14, 14))


class StatusIndicator(QWidget):
    clicked = Signal()
    _reported = Signal(str, str)  # from any thread -> UI thread
    _progressed = Signal(str)

    def __init__(self, pending_count: Callable[[], int], parent=None):
        super().__init__(parent)
        self._pending_count = pending_count
        self.state, self.detail = CONNECTING, ""
        self.last_sync: dt.datetime | None = None
        self._activities: dict[int, str] = {}
        self._progress = ""  # how far the running job is ("Syncing Inbox… 1,200 messages")
        self._next = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(5)
        self.dot, self.spinner, self.label = _Dot(), _Spinner(), QLabel()
        for w in (self.dot, self.spinner, self.label):
            lay.addWidget(w)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reported.connect(self._set_connection)
        self._progressed.connect(self._set_progress)
        self.refresh()

    # -- inputs ---------------------------------------------------------------
    def report(self, state: str, detail: str = "") -> None:
        """graph.status_hook: safe to call from worker threads."""
        self._reported.emit(state, detail)

    def progress(self, text: str) -> None:
        """What the running job is doing, in more detail than its name ("Connecting…" is
        often a first sync of a big folder): safe to call from worker threads."""
        self._progressed.emit(text)

    def _set_progress(self, text: str) -> None:
        if self._activities:  # (a late report from a job that has just ended is dropped)
            self._progress = text
            self.refresh()

    def begin(self, label: str) -> int:
        """Something started ("Syncing with server…"); returns a token for end()."""
        self._next += 1
        self._activities[self._next] = label
        self.refresh()
        return self._next

    def end(self, token: int) -> None:
        self._activities.pop(token, None)
        self._progress = ""  # (the next report names what is still running)
        self.refresh()

    def synced(self) -> None:
        self.last_sync = dt.datetime.now()
        self.refresh()

    def _set_connection(self, state: str, detail: str) -> None:
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self.refresh()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    # -- display ----------------------------------------------------------------
    def text(self) -> str:
        if self._activities:
            return self._progress or list(self._activities.values())[-1]
        pending = self._pending_count()
        waiting = (f"{pending} change{'s' if pending != 1 else ''} waiting" if pending else "")
        if self.state == graph.OFFLINE:
            return "Offline" + (f" · {waiting}" if waiting else "")
        if self.state == graph.SIGNIN:
            return "Sign-in needed"
        if self.state == CONNECTING:
            return "Connecting…"
        if waiting:
            return f"Online · {waiting}"
        when = self.last_sync.strftime("%H:%M") if self.last_sync else ""
        return "Up to date" + (f" · {when}" if when else "")

    def tooltip(self) -> str:
        lines = [{graph.ONLINE: "Connected to Microsoft 365",
                  graph.OFFLINE: "Not connected to Microsoft 365",
                  graph.SIGNIN: "Sign-in needed",
                  CONNECTING: "Connecting to Microsoft 365…"}[self.state]]
        if self.detail:
            lines.append(self.detail)
        if self.last_sync:
            lines.append(f"Last synced at {self.last_sync.strftime('%H:%M:%S')}")
        pending = self._pending_count()
        if pending:
            lines.append(f"{pending} change{'s' if pending != 1 else ''} (read, flag, move) "
                         "waiting to be sent")
        if self._activities:
            lines.append("Now: " + ", ".join(self._activities.values()))
        lines.append("Click to sync now")
        return "\n".join(lines)

    def refresh(self) -> None:
        self.dot.color = QColor(COLORS.get(self.state, COLORS[CONNECTING]))
        self.dot.update()
        self.spinner.spin(bool(self._activities))
        self.label.setText(self.text())
        self.setToolTip(self.tooltip())
