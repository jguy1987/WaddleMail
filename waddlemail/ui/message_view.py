"""Showing one message: a header (subject, sender, recipients, date, attachments) over the
sandboxed body.

Used by the reading pane and by MessageWindow, the window a double-click opens.
"""
from __future__ import annotations

import datetime as dt
import html

from PySide6.QtCore import QDate, QDateTime, QLocale, QSize, Qt, QTime, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence, QPalette
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QPushButton, QToolBar, QVBoxLayout, QWidget,
)

from ..attachments import human_size
from ..invites import Invite
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


class AttachmentStrip(QListWidget):
    """A received message's attachments. Double-click (or Enter) opens one; the context menu
    opens, saves the selected, or saves them all. The owner downloads: open_requested /
    save_requested carry the message id and the attachments (Graph's dicts)."""
    open_requested = Signal(str, list)
    save_requested = Signal(str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSpacing(2)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMaximumHeight(64)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemActivated.connect(lambda item: self._emit(self.open_requested, [item]))
        self.message_id: str | None = None
        self.hide()

    def show_attachments(self, message_id: str, items: list[dict]) -> None:
        self.clear()
        self.message_id = message_id
        for att in items:
            item = QListWidgetItem(iconset.load("paperclip"),
                                   f"{att.get('name') or 'attachment'}  "
                                   f"({human_size(int(att.get('size') or 0))})")
            item.setData(Qt.ItemDataRole.UserRole, att)
            item.setToolTip("Double-click to open; right-click to save")
            self.addItem(item)
        self.setVisible(bool(items))

    def forget(self) -> None:
        self.clear()
        self.message_id = None
        self.hide()

    def attachments(self, items=None) -> list[dict]:
        items = items if items is not None else [self.item(i) for i in range(self.count())]
        return [i.data(Qt.ItemDataRole.UserRole) for i in items]

    def _emit(self, signal, items) -> None:
        if self.message_id and items:
            signal.emit(self.message_id, self.attachments(items))

    def _menu(self, pos) -> None:
        chosen = self.selectedItems() or ([self.itemAt(pos)] if self.itemAt(pos) else [])
        menu = QMenu(self)
        if chosen:
            menu.addAction("&Open", lambda: self._emit(self.open_requested, chosen))
            menu.addAction(iconset.load("save"), "&Save as…" if len(chosen) == 1 else
                           "&Save selected…", lambda: self._emit(self.save_requested, chosen))
        menu.addAction("Save &all…", lambda: self._emit(self.save_requested,
                                                       [self.item(i) for i in range(self.count())]))
        menu.exec(self.viewport().mapToGlobal(pos))


def _time_text(t: dt.datetime) -> str:
    return QLocale.system().toString(QTime(t.hour, t.minute), QLocale.FormatType.ShortFormat)


def _day_text(t: dt.datetime) -> str:
    return QLocale.system().toString(QDate(t.year, t.month, t.day),
                                     QLocale.FormatType.LongFormat)


def span_text(start: dt.datetime, end: dt.datetime, all_day: bool = False) -> str:
    """'Thu, Nov 19, 2026  3:00 PM - 4:00 PM' (all-day: the day or days)."""
    if all_day:
        last = end - dt.timedelta(days=1)
        return _day_text(start) + ("" if last.date() <= start.date()
                                   else f" - {_day_text(last)}")
    if start.date() == end.date():
        return f"{_day_text(start)}   {_time_text(start)} - {_time_text(end)}"
    return f"{_day_text(start)} {_time_text(start)} - {_day_text(end)} {_time_text(end)}"


def when_text(inv: Invite) -> str:
    return span_text(inv.start, inv.end, inv.all_day)


def changes_text(inv: Invite) -> str:
    """What an update changed ('Time changed. Was: ...'), one per line; empty for a new invite."""
    out = []
    if inv.time_changed:
        out.append("Time changed. Was: " + span_text(inv.was_start, inv.was_end or inv.was_start,
                                                     inv.all_day))
    if inv.location_changed:
        out.append(f"Location changed. Was: {inv.was_location or 'none'}")
    return "\n".join(out)


class InviteBar(QFrame):
    """What a meeting invitation says (when, where, who, your answer) with Accept, Tentative
    and Decline. The owner answers: respond_requested carries the message id, the event id and
    the response (accept / tentativelyAccept / decline)."""
    respond_requested = Signal(str, str, str)

    BUTTONS = (("accept", "Accept", "check", "accepted"),
               ("tentativelyAccept", "Tentative", "circle-help", "tentativelyAccepted"),
               ("decline", "Decline", "x", "declined"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)
        self.when = QLabel()
        font = QFont(self.font())
        font.setBold(True)
        self.when.setFont(font)
        self.where = QLabel()
        self.changes = QLabel()  # what an update changed
        self.changes.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.status = QLabel()
        self.status.setWordWrap(True)
        for w in (self.when, self.changes, self.where, self.status):
            w.setTextFormat(Qt.TextFormat.PlainText)  # (the texts come from the server)
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(w)
        row = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        for response, text, icon, _answered in self.BUTTONS:
            b = QPushButton(iconset.load(icon), text)
            b.clicked.connect(lambda _=False, r=response: self._clicked(r))
            row.addWidget(b)
            self.buttons[response] = b
        row.addStretch(1)
        self.buttons_row = row
        lay.addLayout(row)
        self.message_id: str | None = None
        self.invite: Invite | None = None
        self.hide()

    def show_invite(self, message_id: str, inv: Invite) -> None:
        self.message_id, self.invite = message_id, inv
        self.when.setText(when_text(inv))
        changes = changes_text(inv)
        self.changes.setText(changes)
        self.changes.setVisible(bool(changes))
        self.where.setText(inv.location)
        self.where.setVisible(bool(inv.location))
        self.status.setText(inv.status_text())
        for response, _text, _icon, answered in self.BUTTONS:
            b = self.buttons[response]
            b.setVisible(inv.can_respond)
            b.setEnabled(inv.response != answered)  # (you can change your answer)
        self.show()

    def set_busy(self, busy: bool) -> None:
        for b in self.buttons.values():
            b.setEnabled(not busy and self.invite is not None)

    def forget(self) -> None:
        self.message_id = self.invite = None
        self.hide()

    def _clicked(self, response: str) -> None:
        if self.message_id and self.invite:
            self.set_busy(True)  # until the answer is in (show_invite then sets them again)
            self.respond_requested.emit(self.message_id, self.invite.event_id, response)


class MessageView(QWidget):
    """Header + body. The owner loads the body and hands it over with set_body()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = MessageHeader()
        self.invite = InviteBar()
        self.header.layout().addWidget(self.invite)
        self.attachments = AttachmentStrip()
        self.header.layout().addWidget(self.attachments)
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
        self.attachments.forget()
        self.invite.forget()
        self.header.hide()
        self._line.hide()
        self.reader.setHtml(placeholder(text))

    def show_message(self, row) -> None:
        """Show the header right away; the body follows via set_body()."""
        self.message_id = row["id"]
        self._body = None
        self.attachments.forget()
        self.invite.forget()
        self.header.show_message(row)
        self.header.show()
        self._line.show()
        self.reader.setHtml(placeholder("Loading…"))

    def set_body(self, content_type: str, content: str, remote_images: bool) -> None:
        self._body = (content_type, content)
        self.rerender(remote_images)

    def set_attachments(self, message_id: str, items: list[dict]) -> None:
        if message_id == self.message_id:  # (not if the user has moved on)
            self.attachments.show_attachments(message_id, items)

    def set_invite(self, message_id: str, inv: Invite | None) -> None:
        if message_id == self.message_id and inv is not None:
            self.invite.show_invite(message_id, inv)

    def rerender(self, remote_images: bool) -> None:
        if self._body:
            self.reader.setHtml(render_body(*self._body, remote_images))


class MessageWindow(QMainWindow):
    """One message in its own window (double-click or Enter in the list)."""

    def __init__(self, row, remote_images: bool, reply_actions: list[QAction], move_copy,
                 parent=None, respond=None):
        """`move_copy(copy: bool)` asks for a folder and moves (or copies) this message;
        `respond(kind)` replies / replies all / forwards it (kind: reply, replyAll,
        forward). `reply_actions` are shown as they are (placeholders)."""
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
        if respond is not None:  # this window's own Reply / Reply all / Forward
            for icon, text, kind, key in (("reply", "Reply", "reply", "Ctrl+R"),
                                          ("reply-all", "Reply all", "replyAll", "Ctrl+Shift+R"),
                                          ("forward", "Forward", "forward", "Ctrl+F")):
                act = QAction(iconset.load(icon), text, self)
                act.setShortcut(QKeySequence(key))
                act.setToolTip(f"{text} ({key})")
                act.triggered.connect(lambda _=False, k=kind: respond(k))
                bar.addAction(act)
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

    def set_attachments(self, items: list[dict]) -> None:
        if not self.closed:
            self.view.set_attachments(self.view.message_id, items)

    def set_invite(self, message_id: str, inv: Invite | None) -> None:
        if not self.closed:
            self.view.set_invite(message_id, inv)

    def set_body(self, content_type: str, content: str) -> None:
        if not self.closed:
            self.view.set_body(content_type, content, self.images.isChecked())

    def show_error(self, msg: str) -> None:
        if not self.closed:
            self.view.reader.setHtml(placeholder(f"Couldn't load this message: {msg}"))

    def closeEvent(self, event):
        self.closed = True
        super().closeEvent(event)
