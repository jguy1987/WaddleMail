"""Message list: a sortable table model, the view, and the Thunderbird-style quick filter bar.

Filtering happens in SQL (Store.list_messages); sorting happens here, on whichever column the
user clicks. Columns can be dragged into another order and hidden from the header's context
menu; MainWindow saves the header state between runs.

With the reading pane on the right the list is too narrow for columns, so it switches to
compact two-line rows (sender and date over subject and icons) and a Sort menu replaces the
header.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel, QDate, QDateTime, QLocale, QModelIndex, QRect, QSize,
    QSortFilterProxyModel, Qt, QTime, QTimer, Signal,
)
from PySide6.QtGui import QActionGroup, QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QTreeView, QWidget,
)

from ..store import recipients
from . import iconset

ID_ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_ROLE = Qt.ItemDataRole.UserRole + 2  # the whole row, for the compact delegate

# Folders whose interesting person is the recipient, so the From column shows To instead.
OUTGOING = {"sentitems", "drafts", "outbox"}

VERB_REPLIED, VERB_REPLIED_ALL, VERB_FORWARDED = 102, 103, 104


@dataclass(frozen=True)
class Column:
    key: str
    title: str          # header text; empty for icon columns (the icon/tooltip says it)
    icon: str = ""      # header icon for narrow icon columns
    hidden: bool = False


COLUMNS = [
    Column("read", "Read", "mail"),
    Column("status", "Replied / forwarded", "reply"),
    Column("attach", "Attachment", "paperclip"),
    Column("importance", "Importance", "circle-alert"),
    Column("from", "From"),
    Column("to", "To", hidden=True),
    Column("subject", "Subject"),
    Column("received", "Received"),
    Column("size", "Size", hidden=True),
    Column("flag", "Flag", "flag"),
]
COL = {c.key: i for i, c in enumerate(COLUMNS)}
ICON_COLUMNS = {i for i, c in enumerate(COLUMNS) if c.icon}
TOGGLE_COLUMNS = {COL["read"], COL["flag"]}  # a click on these toggles instead of selecting


def _people(value: str | None) -> str:
    return ", ".join(name or addr for name, addr in recipients(value))


def _fmt_received(iso: str | None) -> str:
    """Time for today, weekday + time this week, else the locale's short date."""
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    loc = QLocale.system()
    days = (dt.date.today() - t.date()).days
    clock = loc.toString(QTime(t.hour, t.minute), QLocale.FormatType.ShortFormat)
    if days == 0:
        return clock
    if 0 < days < 7:
        return f"{loc.dayName(t.isoweekday(), QLocale.FormatType.ShortFormat)} {clock}"
    return loc.toString(QDate(t.year, t.month, t.day), QLocale.FormatType.ShortFormat)


def _fmt_size(n: int | None) -> str:
    if not n:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _dot(color: QColor, filled: bool) -> QIcon:
    """The read-status dot: filled accent for unread, a small grey ring for read."""
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if filled:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(4, 4, 8, 8)
    else:
        p.setPen(color)
        p.drawEllipse(6, 6, 4, 4)
    p.end()
    return QIcon(pm)


class MessageModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._index: dict[str, int] = {}
        self.outgoing = False
        pal = QApplication.palette()
        self._accent = pal.color(QPalette.ColorRole.Highlight)
        self._dim = pal.color(QPalette.ColorRole.PlaceholderText)
        self._icons = {
            "unread": _dot(self._accent, True), "read": _dot(self._dim, False),
            VERB_REPLIED: iconset.load("reply"), VERB_REPLIED_ALL: iconset.load("reply-all"),
            VERB_FORWARDED: iconset.load("forward"), "attach": iconset.load("paperclip"),
            "flagged": iconset.load_colored("flag", "#d64545"),
            "complete": iconset.load_colored("check", "#2f9e44"),
        }
        self._bold = QFont()
        self._bold.setBold(True)

    # -- contents ---------------------------------------------------------
    def set_rows(self, rows, outgoing: bool) -> None:
        self.beginResetModel()
        self._rows = [dict(r) for r in rows]
        self._index = {r["id"]: i for i, r in enumerate(self._rows)}
        self.outgoing = outgoing
        self.endResetModel()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, COL["from"], COL["from"])

    def update_rows(self, rows) -> None:
        """Refresh rows in place (after marking read, flagging), keeping selection and scroll."""
        for r in rows:
            i = self._index.get(r["id"])
            if i is not None:
                self._rows[i].update(dict(r))
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLUMNS) - 1))

    def row(self, i: int) -> dict:
        return self._rows[i]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    # -- presentation --------------------------------------------------------
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        col = COLUMNS[section]
        title = "To" if col.key == "from" and self.outgoing else col.title
        if role == Qt.ItemDataRole.DisplayRole:
            return "" if col.icon else title
        if role == Qt.ItemDataRole.DecorationRole and col.icon:
            return iconset.load(col.icon)
        if role == Qt.ItemDataRole.ToolTipRole:
            return title
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        key = COLUMNS[index.column()].key
        if role == ID_ROLE:
            return r["id"]
        if role == ROW_ROLE:
            return r
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(r, key)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._icon(r, key)
        if role == SORT_ROLE:
            return self._sort_key(r, key)
        if role == Qt.ItemDataRole.FontRole and not r["is_read"]:
            return self._bold
        if role == Qt.ItemDataRole.ForegroundRole:
            if key == "importance" and r["importance"] == "high":
                return QColor("#d64545")
            if key in ("received", "size", "importance"):
                return self._dim
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tip(r, key)
        if role == Qt.ItemDataRole.TextAlignmentRole and key in ("size", "importance"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) \
                if key == "size" else int(Qt.AlignmentFlag.AlignCenter)
        return None

    def icon(self, r, key):
        return self._icon(r, key)

    def text(self, r, key) -> str:
        return self._text(r, key) or ""

    def _correspondent(self, r) -> str:
        if self.outgoing:
            return _people(r["to_recipients"]) or "(no recipients)"
        return r["sender_name"] or r["sender_addr"] or ""

    def _text(self, r, key):
        if key == "from":
            return self._correspondent(r)
        if key == "to":
            return _people(r["to_recipients"])
        if key == "subject":
            return r["subject"] or "(no subject)"
        if key == "received":
            return _fmt_received(r["received"])
        if key == "size":
            return _fmt_size(r["size"])
        if key == "importance":
            return {"high": "!", "low": "↓"}.get(r["importance"] or "", "")
        return None

    def _icon(self, r, key):
        if key == "read":
            return self._icons["read" if r["is_read"] else "unread"]
        if key == "status":
            return self._icons.get(r["last_verb"])
        if key == "attach" and r["has_attachments"]:
            return self._icons["attach"]
        if key == "flag":
            return self._icons.get(r["flag"])
        return None

    def _sort_key(self, r, key):
        if key == "read":
            return int(bool(r["is_read"]))
        if key == "status":
            return r["last_verb"] or 0
        if key == "attach":
            return int(bool(r["has_attachments"]))
        if key == "importance":
            return {"low": 0, "high": 2}.get(r["importance"] or "", 1)
        if key == "flag":
            return {"flagged": 2, "complete": 1}.get(r["flag"] or "", 0)
        if key == "received":
            return r["received"] or ""
        if key == "size":
            return r["size"] or 0
        return (self._text(r, key) or "").casefold()

    @staticmethod
    def _tip(r, key):
        if key == "read":
            return ("Read" if r["is_read"] else "Unread") + " (click or press M to toggle)"
        if key == "flag":
            return {"flagged": "Flagged", "complete": "Completed"}.get(
                r["flag"] or "", "Not flagged") + " (click or press S to toggle)"
        if key == "status":
            return {VERB_REPLIED: "Replied", VERB_REPLIED_ALL: "Replied to all",
                    VERB_FORWARDED: "Forwarded"}.get(r["last_verb"])
        if key == "attach" and r["has_attachments"]:
            return "Has attachments"
        if key == "importance" and r["importance"] in ("high", "low"):
            return f"{r['importance'].capitalize()} importance"
        if key == "subject":
            return r["preview"] or None
        if key == "received" and r["received"]:
            t = dt.datetime.fromisoformat(r["received"].replace("Z", "+00:00")).astimezone()
            return QLocale.system().toString(
                QDateTime(QDate(t.year, t.month, t.day), QTime(t.hour, t.minute)), QLocale.FormatType.LongFormat)
        return None


class CompactDelegate(QStyledItemDelegate):
    """Two lines per message: [dot] sender ... date / subject ... icons."""

    PAD = 6
    GUTTER = 18  # left strip with the unread dot

    def __init__(self, model: MessageModel, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index):
        h = option.fontMetrics.height()
        return QSize(200, 2 * h + 3 * self.PAD)

    def dot_rect(self, rect: QRect) -> QRect:
        return QRect(rect.left(), rect.top(), self.GUTTER + self.PAD, rect.height())

    def flag_rect(self, rect: QRect) -> QRect:
        """The flag icon's spot: bottom right."""
        half = rect.height() // 2
        return QRect(rect.right() - 16 - self.PAD - 4, rect.top() + half, 24, half)

    def paint(self, painter, option, index):
        r = index.data(ROW_ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text, opt.icon = "", QIcon()
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        pal = opt.palette
        text = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        dim = text if selected else pal.color(QPalette.ColorRole.PlaceholderText)
        accent = text if selected else pal.color(QPalette.ColorRole.Highlight)
        unread = not r["is_read"]
        mode = QIcon.Mode.Selected if selected else QIcon.Mode.Normal

        rect = opt.rect.adjusted(0, self.PAD, -self.PAD - 4, -self.PAD)
        line_h = rect.height() // 2
        top = QRect(rect.left() + self.GUTTER + self.PAD, rect.top(),
                    rect.width() - self.GUTTER - self.PAD, line_h)
        bottom = top.translated(0, line_h)
        painter.save()

        if unread:  # the dot, vertically centred on the first line
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            cy = top.center().y()
            painter.drawEllipse(rect.left() + self.PAD + 3, cy - 4, 8, 8)

        base = QFont(opt.font)
        bold = QFont(base)
        bold.setBold(unread)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.92)

        # line 1: sender, date on the right
        date = self.model.text(r, "received")
        painter.setFont(small)
        painter.setPen(accent if unread else dim)
        date_w = QFontMetrics(small).horizontalAdvance(date)
        painter.drawText(top, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, date)
        painter.setFont(bold)
        painter.setPen(text)
        name = QFontMetrics(bold).elidedText(self.model.text(r, "from"),
                                             Qt.TextElideMode.ElideRight, top.width() - date_w - 10)
        painter.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        # line 2: icons from the right (flag, attachment, replied/forwarded), then subject
        x = bottom.right()
        icon_y = bottom.center().y() - 8
        for key in ("flag", "attach", "status"):
            icon = self.model.icon(r, key)
            if key == "flag":
                x -= 16  # keep the flag's spot even when unflagged, so it lines up
                if icon:
                    icon.paint(painter, QRect(x, icon_y, 16, 16), Qt.AlignmentFlag.AlignCenter, mode)
                x -= 4
            elif icon:
                x -= 16
                icon.paint(painter, QRect(x, icon_y, 16, 16), Qt.AlignmentFlag.AlignCenter, mode)
                x -= 4
        if r["importance"] == "high":
            painter.setFont(bold)
            painter.setPen(text if selected else QColor("#d64545"))
            x -= 10
            painter.drawText(QRect(x, bottom.top(), 10, bottom.height()),
                             Qt.AlignmentFlag.AlignCenter, "!")
            x -= 4
        subject_rect = QRect(bottom.left(), bottom.top(), x - bottom.left() - 4, bottom.height())
        sfont = QFont(base)
        sfont.setBold(unread)
        painter.setFont(sfont)
        painter.setPen(accent if unread else text)
        subject = QFontMetrics(sfont).elidedText(self.model.text(r, "subject"),
                                                 Qt.TextElideMode.ElideRight, subject_rect.width())
        painter.drawText(subject_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         subject)
        painter.restore()


SORTABLE = ["received", "from", "subject", "size", "importance", "flag", "attach", "read",
            "status"]


class MessageListView(QTreeView):
    openRequested = Signal(str)
    toggleReadRequested = Signal(list)
    toggleFlagRequested = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = MessageModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(SORT_ROLE)
        self.setModel(self.proxy)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setIconSize(QSize(16, 16))
        self.setSortingEnabled(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.doubleClicked.connect(self._on_double_clicked)

        h = self.header()
        h.setSectionsMovable(True)
        h.setStretchLastSection(False)
        h.setHighlightSections(False)
        h.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        h.customContextMenuRequested.connect(self._header_menu)
        self.reset_columns()

        self.compact = False
        self._compact_delegate = CompactDelegate(self.model_, self)
        self._wide_state = None

    # -- compact (two-line) mode ----------------------------------------
    def set_compact(self, on: bool) -> None:
        if on == self.compact:
            return
        h = self.header()
        sort = (h.sortIndicatorSection(), h.sortIndicatorOrder())
        if on:
            self._wide_state = h.saveState()
            for i in range(len(COLUMNS)):
                h.setSectionHidden(i, i != COL["subject"])
            h.setSectionResizeMode(COL["subject"], QHeaderView.ResizeMode.Stretch)
            h.hide()
            self.setItemDelegateForColumn(COL["subject"], self._compact_delegate)
            self.setAlternatingRowColors(False)
        else:
            self.setItemDelegateForColumn(COL["subject"], None)
            if self._wide_state is not None:
                h.restoreState(self._wide_state)
            h.show()
            self.setAlternatingRowColors(True)
        self.compact = on
        self.sortByColumn(*sort)  # restoring the header must not undo a sort chosen meanwhile
        self.doItemsLayout()

    def header_state(self):
        """The column layout to save: the wide one, even while compact."""
        if self.compact and self._wide_state is not None:
            h = self.header()
            # carry the current sort over into the saved wide layout
            self.set_compact(False)
            state = h.saveState()
            self.set_compact(True)
            return state
        return self.header().saveState()

    def sort_menu(self) -> QMenu:
        """Sort by any column, and the direction: used where there is no header to click."""
        h = self.header()
        menu = QMenu(self)
        group = QActionGroup(menu)
        for key in SORTABLE:
            col = COL[key]
            title = self.model_.headerData(col, Qt.Orientation.Horizontal,
                                           Qt.ItemDataRole.ToolTipRole)
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(h.sortIndicatorSection() == col)
            act.triggered.connect(lambda _=False, c=col: self.sortByColumn(
                c, Qt.SortOrder.DescendingOrder if c == COL["received"] else
                Qt.SortOrder.AscendingOrder))
            group.addAction(act)
        menu.addSeparator()
        order = QActionGroup(menu)
        for label, value in (("Ascending", Qt.SortOrder.AscendingOrder),
                             ("Descending", Qt.SortOrder.DescendingOrder)):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(h.sortIndicatorOrder() == value)
            act.triggered.connect(lambda _=False, v=value: self.sortByColumn(
                h.sortIndicatorSection(), v))
            order.addAction(act)
        return menu

    def sort_label(self) -> str:
        h = self.header()
        title = self.model_.headerData(h.sortIndicatorSection(), Qt.Orientation.Horizontal,
                                       Qt.ItemDataRole.ToolTipRole)
        arrow = "↓" if h.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder else "↑"
        return f"{title} {arrow}"

    def reset_columns(self) -> None:
        """Default order, widths and visibility; newest mail first."""
        h = self.header()
        for logical in range(len(COLUMNS)):  # undo any reordering
            h.moveSection(h.visualIndex(logical), logical)
        for i, col in enumerate(COLUMNS):
            if i in ICON_COLUMNS:
                h.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
                h.resizeSection(i, 26)
            else:
                h.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            h.setSectionHidden(i, col.hidden)
        h.setSectionResizeMode(COL["subject"], QHeaderView.ResizeMode.Stretch)
        h.resizeSection(COL["from"], 190)
        h.resizeSection(COL["to"], 190)
        h.resizeSection(COL["received"], 120)
        h.resizeSection(COL["size"], 70)
        self.sortByColumn(COL["received"], Qt.SortOrder.DescendingOrder)

    def restore_header(self, state) -> None:
        if state and not state.isEmpty() and self.header().restoreState(state):
            h = self.header()
            self.sortByColumn(h.sortIndicatorSection(), h.sortIndicatorOrder())

    def _header_menu(self, pos):
        h = self.header()
        menu = QMenu(self)
        for i, col in enumerate(COLUMNS):
            if col.key == "subject":
                continue  # always shown
            act = menu.addAction(self.model_.headerData(i, Qt.Orientation.Horizontal,
                                                        Qt.ItemDataRole.ToolTipRole))
            act.setCheckable(True)
            act.setChecked(not h.isSectionHidden(i))
            act.toggled.connect(lambda on, i=i: h.setSectionHidden(i, not on))
        menu.addSeparator()
        menu.addAction("Restore default columns", self.reset_columns)
        menu.exec(h.mapToGlobal(pos))

    # -- ids --------------------------------------------------------------
    def selected_ids(self) -> list[str]:
        rows = sorted(i.row() for i in self.selectionModel().selectedRows())
        return [self.proxy.index(r, 0).data(ID_ROLE) for r in rows]

    def current_id(self) -> str | None:
        idx = self.currentIndex()
        return idx.data(ID_ROLE) if idx.isValid() else None

    def set_messages(self, rows, outgoing: bool) -> None:
        """Replace the contents, keeping the selection, current row and scroll position."""
        keep, current = set(self.selected_ids()), self.current_id()
        scroll = self.verticalScrollBar().value()
        sm = self.selectionModel()
        sm.blockSignals(True)
        self.model_.set_rows(rows, outgoing)
        for r in range(self.proxy.rowCount()):
            idx = self.proxy.index(r, 0)
            mid = idx.data(ID_ROLE)
            if mid == current:
                sm.setCurrentIndex(idx, sm.SelectionFlag.NoUpdate)
            if mid in keep:
                sm.select(idx, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
        sm.blockSignals(False)
        self.verticalScrollBar().setValue(scroll)

    # -- input ------------------------------------------------------------
    def _toggle_target(self, event) -> str | None:
        """"read" or "flag" when a plain left click lands on a toggle spot."""
        if event.button() != Qt.MouseButton.LeftButton or event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            return None
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        if self.compact:
            rect = self.visualRect(idx)
            if self._compact_delegate.dot_rect(rect).contains(pos):
                return "read"
            if self._compact_delegate.flag_rect(rect).contains(pos):
                return "flag"
            return None
        return {COL["read"]: "read", COL["flag"]: "flag"}.get(idx.column())

    def mousePressEvent(self, event):
        target = self._toggle_target(event)
        if target:
            mid = self.indexAt(event.position().toPoint()).data(ID_ROLE)
            # Toggle the clicked message without changing the selection (Thunderbird does
            # the same); within a multi-selection, toggle the whole selection.
            ids = self.selected_ids() if mid in self.selected_ids() else [mid]
            if target == "read":
                self.toggleReadRequested.emit(ids)
            else:
                self.toggleFlagRequested.emit(ids)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._toggle_target(event):
            return  # two quick toggles, not an open
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.current_id():
            self.openRequested.emit(self.current_id())
            return
        super().keyPressEvent(event)

    def _on_double_clicked(self, idx):
        if idx.isValid():
            self.openRequested.emit(idx.data(ID_ROLE))


class FilterBar(QWidget):
    """Quick filter: Unread / Flagged / Attachment toggles plus one search box.
    Every word typed must match the sender, recipients, subject or preview."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        self.unread = self._toggle("Unread", "mail", "Only unread messages")
        self.flagged = self._toggle("Flagged", "flag", "Only flagged messages")
        self.attachments = self._toggle("Attachment", "paperclip", "Only messages with attachments")
        for b in (self.unread, self.flagged, self.attachments):
            lay.addWidget(b)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Filter these messages <Ctrl+Shift+K>")
        self.search.addAction(iconset.load("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setToolTip("Every word must appear in the sender, recipients, subject or "
                               "preview. Esc clears.")
        lay.addWidget(self.search, 1)
        self.sort_button = QToolButton()  # shown in compact mode, where there is no header
        self.sort_button.setAutoRaise(True)
        self.sort_button.setToolTip("Sort messages")
        self.sort_button.hide()
        lay.addWidget(self.sort_button)
        self.count = QLabel()
        self.count.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(self.count)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self.changed)
        self.search.textChanged.connect(lambda _: self._debounce.start())

    def _toggle(self, text: str, icon: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setIcon(iconset.load(icon))
        b.setToolTip(tip)
        b.setCheckable(True)
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.toggled.connect(lambda _: self.changed.emit())
        return b

    def set_compact(self, on: bool):
        """Icon-only toggles and a Sort button, for a narrow list."""
        style = (Qt.ToolButtonStyle.ToolButtonIconOnly if on
                 else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for b in (self.unread, self.flagged, self.attachments):
            b.setToolButtonStyle(style)
        self.sort_button.setVisible(on)
        self.count.setVisible(not on)

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            return
        super().keyPressEvent(event)

    def active(self) -> bool:
        return bool(self.search.text().strip() or self.unread.isChecked()
                    or self.flagged.isChecked() or self.attachments.isChecked())

    def criteria(self) -> dict:
        if self.isHidden():
            return {}
        return {"search": self.search.text().strip(), "unread": self.unread.isChecked(),
                "flagged": self.flagged.isChecked(), "attachments": self.attachments.isChecked()}

    def clear(self, emit: bool = True):
        """Reset all criteria, with (at most) one `changed` signal."""
        for w in (self.search, self.unread, self.flagged, self.attachments):
            w.blockSignals(True)
        self.search.clear()
        for b in (self.unread, self.flagged, self.attachments):
            b.setChecked(False)
        for w in (self.search, self.unread, self.flagged, self.attachments):
            w.blockSignals(False)
        self._debounce.stop()
        if emit:
            self.changed.emit()

    def set_count(self, shown: int):
        word = "match" if shown == 1 else "matches"
        if not self.active():
            word = "message" if shown == 1 else "messages"
        self.count.setText(f"{shown} {word}")
