"""Showing one message: a header (subject, sender, recipients, date) over the sandboxed body.

Used by the reading pane and by MessageWindow, the window a double-click opens.
"""
from __future__ import annotations

import datetime as dt
import html

from PySide6.QtCore import QDate, QDateTime, QLocale, Qt, QTime
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence, QPalette
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFrame, QLabel, QMainWindow, QToolBar, QVBoxLayout, QWidget

from ..store import recipients
from . import iconset
from .render import placeholder, render_body


class ReaderPage(QWebEnginePage):
    """Opens clicked links in the system browser instead of navigating the reader."""

    def javaScriptConsoleMessage(self, level, message, line, source):
        # Blocked remote images are expected (see the CSP); don't spam the terminal with them.
        if "Content Security Policy" in message:
            return
        super().javaScriptConsoleMessage(level, message, line, source)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            if url.scheme() in ("http", "https", "mailto"):
                QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


def make_reader(parent=None) -> QWebEngineView:
    """No JavaScript, no plugins; the CSP in render.py blocks remote content by default."""
    reader = QWebEngineView(parent)
    reader.setPage(ReaderPage(reader))
    s = reader.settings()
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    return reader


def _person(name: str, addr: str) -> str:
    if name and addr and name != addr:
        return f"<b>{html.escape(name)}</b> &lt;{html.escape(addr)}&gt;"
    return f"<b>{html.escape(name or addr)}</b>"


def _long_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return QLocale.system().toString(QDateTime(QDate(t.year, t.month, t.day), QTime(t.hour, t.minute)),
                                     QLocale.FormatType.LongFormat)


class MessageHeader(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(3)
        self.subject = self._label()
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.35)
        font.setBold(True)
        self.subject.setFont(font)
        self.sender, self.to, self.cc, self.date = (self._label() for _ in range(4))
        self.date.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        for w in (self.subject, self.sender, self.to, self.cc, self.date):
            lay.addWidget(w)

    @staticmethod
    def _label() -> QLabel:
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lbl

    def show_message(self, row) -> None:
        self.subject.setText(html.escape(row["subject"] or "(no subject)"))
        self.sender.setText(_person(row["sender_name"] or "", row["sender_addr"] or ""))
        for label, prefix, value in ((self.to, "To", row["to_recipients"]),
                                     (self.cc, "Cc", row["cc_recipients"])):
            people = recipients(value)
            label.setVisible(bool(people))
            label.setText(f"{prefix}: " + ", ".join(html.escape(n or a) for n, a in people))
            label.setToolTip("\n".join(f"{n} <{a}>" if n else a for n, a in people))
        self.date.setText(html.escape(_long_date(row["received"])))


class MessageView(QWidget):
    """Header + body. The owner loads the body and hands it over with set_body()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = MessageHeader()
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        self._line = line
        self.reader = make_reader()
        lay.addWidget(self.header)
        lay.addWidget(line)
        lay.addWidget(self.reader, 1)
        self._body: tuple[str, str] | None = None
        self.message_id: str | None = None
        self.show_placeholder("Select a message.")

    def show_placeholder(self, text: str) -> None:
        self.message_id = None
        self._body = None
        self.header.hide()
        self._line.hide()
        self.reader.setHtml(placeholder(text))

    def show_message(self, row) -> None:
        """Show the header right away; the body follows via set_body()."""
        self.message_id = row["id"]
        self._body = None
        self.header.show_message(row)
        self.header.show()
        self._line.show()
        self.reader.setHtml(placeholder("Loading…"))

    def set_body(self, content_type: str, content: str, remote_images: bool) -> None:
        self._body = (content_type, content)
        self.rerender(remote_images)

    def rerender(self, remote_images: bool) -> None:
        if self._body:
            self.reader.setHtml(render_body(*self._body, remote_images))


class MessageWindow(QMainWindow):
    """One message in its own window (double-click or Enter in the list)."""

    def __init__(self, row, remote_images: bool, reply_actions: list[QAction], move_copy,
                 parent=None):
        """`move_copy(copy: bool)` asks for a folder and moves (or copies) this message."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(row["subject"] or "(no subject)")
        self.resize(860, 720)
        self.closed = False
        self.view = MessageView()
        self.view.show_message(row)
        self.setCentralWidget(self.view)

        bar = QToolBar("Message", self)
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for act in reply_actions:
            bar.addAction(act)
        bar.addSeparator()
        self.act_move = QAction(iconset.load("folder-input"), "Mo&ve to folder…", self)
        self.act_move.setIconText("Move")  # the toolbar's label
        self.act_move.setToolTip("Move to folder (Ctrl+Shift+V)")
        self.act_move.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.act_move.triggered.connect(lambda: move_copy(False))
        self.act_copy = QAction(iconset.load("copy"), "Cop&y to folder…", self)
        self.act_copy.setIconText("Copy")
        self.act_copy.setToolTip("Copy to folder")
        self.act_copy.triggered.connect(lambda: move_copy(True))
        bar.addAction(self.act_move)
        bar.addAction(self.act_copy)
        bar.addSeparator()
        self.images = QAction(iconset.load("image"), "Load images", self)
        self.images.setCheckable(True)
        self.images.setChecked(remote_images)
        self.images.setToolTip("Load remote images in this message")
        self.images.toggled.connect(self.view.rerender)
        bar.addAction(self.images)
        self.addToolBar(bar)

        close = QAction("&Close", self)
        close.setShortcuts([QKeySequence(Qt.Key.Key_Escape), QKeySequence.StandardKey.Close])
        close.triggered.connect(self.close)
        self.addAction(close)

        menu = self.menuBar().addMenu("&Message")
        menu.addAction(self.act_move)
        menu.addAction(self.act_copy)
        menu.addSeparator()
        menu.addAction(self.images)
        menu.addSeparator()
        menu.addAction(close)

    @property
    def message_id(self) -> str | None:
        return self.view.message_id

    def set_body(self, content_type: str, content: str) -> None:
        if not self.closed:
            self.view.set_body(content_type, content, self.images.isChecked())

    def show_error(self, msg: str) -> None:
        if not self.closed:
            self.view.reader.setHtml(placeholder(f"Couldn't load this message: {msg}"))

    def closeEvent(self, event):
        self.closed = True
        super().closeEvent(event)
