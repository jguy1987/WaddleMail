"""Message list: a sortable tree model, the view, and the Thunderbird-style quick filter bar.

The list is a tree of up to three levels:

    date heading ("Today", "Last week", ...)     only while sorted by Received
      conversation (participants, count)         when two or more messages share one
        message

Filtering happens in SQL (Store.list_messages); sorting happens here, on whichever column the
user clicks, at every level. Columns can be dragged into another order and hidden from the
header's context menu; MainWindow saves the header state between runs.

With the reading pane on the right the list is too narrow for columns, so it switches to
compact two-line rows (sender and date over subject and icons) and a Sort menu replaces the
header.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractItemModel, QDate, QDateTime, QLocale, QModelIndex, QRect, QSize,
    QSortFilterProxyModel, Qt, QTime, QTimer, Signal,
)
from PySide6.QtGui import (
    QActionGroup, QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QTreeView, QWidget,
)

from ..store import recipients
from . import iconset

ID_ROLE = Qt.ItemDataRole.UserRole       # message id (messages only)
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
NODE_ROLE = Qt.ItemDataRole.UserRole + 2  # the Node behind a row

# Folders whose interesting person is the recipient, so the From column shows To instead.
OUTGOING = {"sentitems", "drafts", "outbox"}

VERB_REPLIED, VERB_REPLIED_ALL, VERB_FORWARDED = 102, 103, 104
GROUP, THREAD, MESSAGE = "group", "thread", "message"


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

_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|wg|tr|antw|vs)\s*(\[\d+\])?\s*:\s*)+",
                           re.IGNORECASE)


def thread_subject(subject: str | None) -> str:
    """'RE: FW: Budget' -> 'Budget'."""
    return _REPLY_PREFIX.sub("", subject or "").strip() or "(no subject)"


def _people(value: str | None) -> str:
    return ", ".join(name or addr for name, addr in recipients(value))


def _local(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _fmt_received(iso: str | None) -> str:
    """Time for today, weekday + time this week, else the locale's short date."""
    t = _local(iso)
    if t is None:
        return iso or ""
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


def date_group(iso: str | None, today: dt.date | None = None,
               first_weekday: int | None = None) -> tuple[int, str]:
    """(rank, heading) for a received time; a higher rank is more recent."""
    today = today or dt.date.today()
    if first_weekday is None:  # 1 = Monday ... 7 = Sunday
        first_weekday = QLocale.system().firstDayOfWeek().value
    t = _local(iso)
    if t is None:
        return 0, "Older"
    d = t.date()
    week_start = today - dt.timedelta(days=(today.isoweekday() - first_weekday) % 7)
    month_start = today.replace(day=1)
    last_month_start = (month_start - dt.timedelta(days=1)).replace(day=1)
    if d >= today:
        return 6, "Today"
    if d == today - dt.timedelta(days=1):
        return 5, "Yesterday"
    if d >= week_start:
        return 4, "This week"
    if d >= week_start - dt.timedelta(days=7):
        return 3, "Last week"
    if d >= month_start:
        return 2, "This month"
    if d >= last_month_start:
        return 1, "Last month"
    return 0, "Older"


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


class Node:
    """A row: a date heading, a conversation, or a message. `row` holds the message's
    columns; for a conversation, a summary in the same shape (see _summary)."""
    __slots__ = ("kind", "key", "row", "label", "rank", "children", "parent", "pos")

    def __init__(self, kind: str, key: str, row: dict | None = None, label: str = "",
                 rank: int = 0):
        self.kind, self.key, self.row, self.label, self.rank = kind, key, row, label, rank
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.pos = 0

    def add(self, child: Node) -> None:
        child.parent, child.pos = self, len(self.children)
        self.children.append(child)

    def messages(self) -> list[dict]:
        if self.kind == MESSAGE:
            return [self.row]
        return [m for c in self.children for m in c.messages()]

    def message_count(self) -> int:
        return 1 if self.kind == MESSAGE else sum(c.message_count() for c in self.children)


def _summary(msgs: list[dict], outgoing: bool) -> dict:
    """A conversation row: newest values for dates/state, any-of for flags and attachments."""
    by_date = sorted(msgs, key=lambda m: m["received"] or "", reverse=True)
    newest, oldest = by_date[0], by_date[-1]
    people, seen = [], set()
    for m in by_date:
        names = ([n or a for n, a in recipients(m["to_recipients"])] if outgoing
                 else [m["sender_name"] or m["sender_addr"] or ""])
        for name in names:
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                people.append(name)
    flags = {m["flag"] for m in msgs}
    return {
        "id": None, "conversation_id": newest["conversation_id"],
        "subject": thread_subject(oldest["subject"]), "participants": ", ".join(people),
        "sender_name": newest["sender_name"], "sender_addr": newest["sender_addr"],
        "to_recipients": newest["to_recipients"], "preview": newest["preview"],
        "received": newest["received"], "last_verb": newest["last_verb"],
        "is_read": int(all(m["is_read"] for m in msgs)),
        "unread_count": sum(1 for m in msgs if not m["is_read"]),
        "has_attachments": int(any(m["has_attachments"] for m in msgs)),
        "importance": "high" if any(m["importance"] == "high" for m in msgs)
        else newest["importance"],
        "flag": "flagged" if "flagged" in flags else ("complete" if "complete" in flags else None),
        "size": sum(m["size"] or 0 for m in msgs), "count": len(msgs), "newest_id": newest["id"],
    }


class MessageModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = Node("root", "")
        self._by_id: dict[str, Node] = {}
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
    def set_rows(self, rows, outgoing: bool, threaded: bool, grouped: bool) -> None:
        self.beginResetModel()
        self.outgoing = outgoing
        self.root = Node("root", "")
        self._by_id = {}
        msgs = [dict(r) for r in rows]

        top: list[Node] = []
        convs: dict[str, list[dict]] = {}
        for m in msgs:
            if threaded and m["conversation_id"]:
                convs.setdefault(m["conversation_id"], []).append(m)
            else:
                top.append(self._message_node(m))
        for conv, members in convs.items():
            if len(members) == 1:
                top.append(self._message_node(members[0]))
                continue
            thread = Node(THREAD, "t:" + conv, _summary(members, outgoing))
            for m in members:
                thread.add(self._message_node(m))
            top.append(thread)

        if grouped:
            groups: dict[int, Node] = {}
            for node in top:
                rank, label = date_group(node.row["received"])
                if rank not in groups:
                    groups[rank] = Node(GROUP, "g:" + label, label=label, rank=rank)
                groups[rank].add(node)
            for rank in sorted(groups, reverse=True):
                self.root.add(groups[rank])
        else:
            for node in top:
                self.root.add(node)
        self.endResetModel()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, COL["from"], COL["from"])

    def _message_node(self, m: dict) -> Node:
        node = Node(MESSAGE, "m:" + m["id"], m)
        self._by_id[m["id"]] = node
        return node

    def update_rows(self, rows) -> None:
        """Refresh messages in place (after marking read, flagging), keeping selection and
        scroll; their conversation rows are recomputed too."""
        last = len(COLUMNS) - 1
        for r in rows:
            node = self._by_id.get(r["id"]) if r else None
            if node is None:
                continue
            node.row.update(dict(r))
            idx = self.createIndex(node.pos, 0, node)
            self.dataChanged.emit(idx, idx.siblingAtColumn(last))
            parent = node.parent
            if parent is not None and parent.kind == THREAD:
                parent.row = _summary(parent.messages(), self.outgoing)
                pidx = self.createIndex(parent.pos, 0, parent)
                self.dataChanged.emit(pidx, pidx.siblingAtColumn(last))

    # -- tree plumbing -----------------------------------------------------
    @staticmethod
    def node(index: QModelIndex) -> Node | None:
        return index.internalPointer() if index.isValid() else None

    def index(self, row, column, parent=QModelIndex()):
        node = self.node(parent) or self.root
        if 0 <= row < len(node.children) and 0 <= column < len(COLUMNS):
            return self.createIndex(row, column, node.children[row])
        return QModelIndex()

    def parent(self, index=None):
        if index is None:  # QObject.parent()
            return super().parent()
        node = self.node(index)
        if node is None or node.parent is None or node.parent is self.root:
            return QModelIndex()
        return self.createIndex(node.parent.pos, 0, node.parent)

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() and parent.column() != 0:
            return 0
        return len((self.node(parent) or self.root).children)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def flags(self, index):
        node = self.node(index)
        if node is None:
            return Qt.ItemFlag.NoItemFlags
        if node.kind == GROUP:  # a heading: not selectable
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

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
        node = self.node(index)
        if node is None:
            return None
        if role == NODE_ROLE:
            return node
        if node.kind == GROUP:
            if role == Qt.ItemDataRole.DisplayRole:
                return node.label
            if role == SORT_ROLE:
                return node.rank
            return None
        r = node.row
        key = COLUMNS[index.column()].key
        if role == ID_ROLE:
            return r["id"]
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
        if r.get("participants") is not None:
            return r["participants"]
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
            if r.get("count"):
                return (f"Conversation: {r['count']} messages, {r['unread_count']} unread\n"
                        + (r["preview"] or ""))
            return r["preview"] or None
        if key == "received" and r["received"]:
            t = _local(r["received"])
            if t is not None:
                return QLocale.system().toString(
                    QDateTime(QDate(t.year, t.month, t.day), QTime(t.hour, t.minute)),
                    QLocale.FormatType.LongFormat)
        return None


def _palette_colors(opt) -> tuple[QColor, QColor, QColor]:
    """(text, dim, accent) for a row, as they read on its background."""
    selected = bool(opt.state & QStyle.StateFlag.State_Selected)
    pal = opt.palette
    text = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
    dim = text if selected else pal.color(QPalette.ColorRole.PlaceholderText)
    accent = text if selected else pal.color(QPalette.ColorRole.Highlight)
    return text, dim, accent


def _paint_group(painter, opt, node: Node) -> None:
    """A date heading: bold label, dim count, a hairline under it."""
    _, dim, accent = _palette_colors(opt)
    rect = opt.rect.adjusted(8, 0, -8, 0)
    painter.save()
    font = QFont(opt.font)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(accent)
    painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, node.label)
    w = QFontMetrics(font).horizontalAdvance(node.label)
    small = QFont(opt.font)
    painter.setFont(small)
    painter.setPen(dim)
    painter.drawText(rect.adjusted(w + 8, 0, 0, 0),
                     Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                     str(node.message_count()))
    line = QColor(dim)
    line.setAlpha(80)
    painter.setPen(line)
    painter.drawLine(opt.rect.left(), opt.rect.bottom(), opt.rect.right(), opt.rect.bottom())
    painter.restore()


def _chevron(expanded: bool) -> QIcon:
    return iconset.load("chevron-down" if expanded else "chevron-right")


class RowDelegate(QStyledItemDelegate):
    """Wide (column) mode: date headings across the row; in the Subject cell, the expand arrow
    and message count of a conversation, and an indent for the messages inside it."""

    ARROW = 18   # room for the conversation arrow
    INDENT = 22  # messages inside a conversation

    def paint(self, painter, option, index):
        node = index.data(NODE_ROLE)
        if node is not None and node.kind == GROUP:
            _paint_group(painter, option, node)
            return
        if node is None or COLUMNS[index.column()].key != "subject":
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        subject, opt.text = opt.text, ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text, dim, _ = _palette_colors(opt)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        rect = opt.rect.adjusted(4, 0, -4, 0)
        painter.save()
        if node.kind == THREAD:
            expanded = widget is not None and widget.isExpanded(index.siblingAtColumn(0))
            _chevron(expanded).paint(
                painter, QRect(rect.left(), rect.center().y() - 8, 16, 16),
                Qt.AlignmentFlag.AlignCenter,
                QIcon.Mode.Selected if selected else QIcon.Mode.Normal)
            rect.setLeft(rect.left() + self.ARROW)
        elif node.parent is not None and node.parent.kind == THREAD:
            rect.setLeft(rect.left() + self.INDENT)
        count = f"{node.row['count']}" if node.kind == THREAD else ""
        fm = QFontMetrics(opt.font)
        count_w = fm.horizontalAdvance(count) + 8 if count else 0
        elided = fm.elidedText(subject, Qt.TextElideMode.ElideRight, rect.width() - count_w)
        painter.setFont(opt.font)
        painter.setPen(text)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        if count:
            small = QFont(opt.font)
            small.setBold(False)
            painter.setFont(small)
            painter.setPen(dim)
            painter.drawText(rect.adjusted(fm.horizontalAdvance(elided) + 6, 0, 0, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             f"({count})")
        painter.restore()

    @staticmethod
    def arrow_rect(cell: QRect) -> QRect:
        return QRect(cell.left(), cell.top(), RowDelegate.ARROW + 6, cell.height())


class CompactDelegate(QStyledItemDelegate):
    """Two lines per message: [dot] sender ... date / [arrow] subject ... icons."""

    PAD = 6
    GUTTER = 18  # left strip with the unread dot (and a conversation's arrow)
    INDENT = 14  # messages inside a conversation

    def __init__(self, model: MessageModel, parent=None):
        super().__init__(parent)
        self.model = model

    def sizeHint(self, option, index):
        h = option.fontMetrics.height()
        node = index.data(NODE_ROLE)
        if node is not None and node.kind == GROUP:
            return QSize(200, h + 2 * self.PAD)
        return QSize(200, 2 * h + 3 * self.PAD)

    def _indent(self, node: Node) -> int:
        return self.INDENT if node.parent is not None and node.parent.kind == THREAD else 0

    def dot_rect(self, rect: QRect, node: Node) -> QRect:
        x = rect.left() + self._indent(node)
        return QRect(x, rect.top(), self.GUTTER + self.PAD, rect.height() // 2)

    def arrow_rect(self, rect: QRect, node: Node) -> QRect:
        x = rect.left() + self._indent(node)
        return QRect(x, rect.top() + rect.height() // 2, self.GUTTER + self.PAD,
                     rect.height() - rect.height() // 2)

    def flag_rect(self, rect: QRect) -> QRect:
        """The flag icon's spot: bottom right."""
        half = rect.height() // 2
        return QRect(rect.right() - 16 - self.PAD - 4, rect.top() + half, 24, half)

    def paint(self, painter, option, index):
        node = index.data(NODE_ROLE)
        if node.kind == GROUP:
            _paint_group(painter, option, node)
            return
        r = node.row
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text, opt.icon = "", QIcon()
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        text, dim, accent = _palette_colors(opt)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        unread = not r["is_read"]
        mode = QIcon.Mode.Selected if selected else QIcon.Mode.Normal

        rect = opt.rect.adjusted(self._indent(node), self.PAD, -self.PAD - 4, -self.PAD)
        line_h = rect.height() // 2
        top = QRect(rect.left() + self.GUTTER + self.PAD, rect.top(),
                    rect.width() - self.GUTTER - self.PAD, line_h)
        bottom = top.translated(0, line_h)
        painter.save()

        if unread:  # the dot, vertically centred on the first line
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(rect.left() + self.PAD + 3, top.center().y() - 4, 8, 8)
        if node.kind == THREAD:
            expanded = widget is not None and widget.isExpanded(index.siblingAtColumn(0))
            _chevron(expanded).paint(
                painter, QRect(rect.left() + self.PAD - 1, bottom.center().y() - 8, 16, 16),
                Qt.AlignmentFlag.AlignCenter, mode)

        base = QFont(opt.font)
        base.setBold(False)
        bold = QFont(base)
        bold.setBold(unread)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.92)

        # line 1: sender(s), date on the right
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
        if node.kind == THREAD:  # message count, like Outlook's conversation header
            count = str(r["count"])
            painter.setFont(small)
            painter.setPen(dim)
            w = QFontMetrics(small).horizontalAdvance(count)
            x -= w
            painter.drawText(QRect(x, bottom.top(), w, bottom.height()),
                             Qt.AlignmentFlag.AlignCenter, count)
            x -= 6
        subject_rect = QRect(bottom.left(), bottom.top(), x - bottom.left() - 4, bottom.height())
        painter.setFont(bold)
        painter.setPen(accent if unread else text)
        subject = QFontMetrics(bold).elidedText(self.model.text(r, "subject"),
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
        # Conversations expand with our own arrow in the Subject cell (the tree's built-in
        # branch arrows would land in whatever narrow column is first).
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setExpandsOnDoubleClick(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setIconSize(QSize(16, 16))
        self.setSortingEnabled(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self._row_delegate = RowDelegate(self)
        self.setItemDelegate(self._row_delegate)
        self.doubleClicked.connect(self._on_double_clicked)

        self.threaded = True
        self.grouped = True
        self._rows: list = []
        self._outgoing = False
        self._expanded: set[str] = set()   # conversation keys the user opened
        self._collapsed: set[str] = set()  # date headings the user closed
        self.expanded.connect(lambda i: self._remember(i, True))
        self.collapsed.connect(lambda i: self._remember(i, False))
        self.proxy.layoutChanged.connect(self._apply_spans)

        h = self.header()
        h.setSectionsMovable(True)
        h.setStretchLastSection(False)
        h.setHighlightSections(False)
        h.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        h.customContextMenuRequested.connect(self._header_menu)
        self.reset_columns()
        h.sortIndicatorChanged.connect(self._on_sort_changed)

        self.compact = False
        self._compact_delegate = CompactDelegate(self.model_, self)
        self._wide_state = None

    # -- columns ----------------------------------------------------------
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
            self.setUniformRowHeights(False)  # headings are shorter than messages
        else:
            self.setItemDelegateForColumn(COL["subject"], None)
            if self._wide_state is not None:
                h.restoreState(self._wide_state)
            h.show()
            self.setAlternatingRowColors(True)
            self.setUniformRowHeights(True)
        self.compact = on
        self.sortByColumn(*sort)  # restoring the header must not undo a sort chosen meanwhile
        self._apply_spans()
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

    # -- conversations and date headings ------------------------------------
    def set_options(self, threaded: bool, grouped: bool) -> None:
        if (threaded, grouped) != (self.threaded, self.grouped):
            self.threaded, self.grouped = threaded, grouped
            self._rebuild()

    def _grouping(self) -> bool:
        """Date headings only make sense while sorted by date."""
        return self.grouped and self.header().sortIndicatorSection() == COL["received"]

    def _on_sort_changed(self, *_):
        if self._grouping() != any(n.kind == GROUP for n in self.model_.root.children):
            self._rebuild()

    def _remember(self, index: QModelIndex, expanded: bool):
        node = index.data(NODE_ROLE)
        if node is None:
            return
        if node.kind == THREAD:
            (self._expanded.add if expanded else self._expanded.discard)(node.key)
        elif node.kind == GROUP:
            (self._collapsed.discard if expanded else self._collapsed.add)(node.key)

    def _walk(self, parent: QModelIndex = QModelIndex()):
        """Every row (column 0) of the sorted view, depth first."""
        for r in range(self.proxy.rowCount(parent)):
            idx = self.proxy.index(r, 0, parent)
            yield idx
            yield from self._walk(idx)

    def _apply_spans(self):
        """Date headings run across all columns (in compact mode there is only one)."""
        for r in range(self.proxy.rowCount()):
            idx = self.proxy.index(r, 0)
            node = idx.data(NODE_ROLE)
            self.setFirstColumnSpanned(r, QModelIndex(),
                                       not self.compact and node is not None
                                       and node.kind == GROUP)

    def set_messages(self, rows, outgoing: bool) -> None:
        self._rows, self._outgoing = list(rows), outgoing
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild the tree, keeping selection, current row, open conversations and scroll."""
        keys = {i.data(NODE_ROLE).key for i in self.selectionModel().selectedRows()
                if i.data(NODE_ROLE) is not None}
        cur = self.currentIndex().data(NODE_ROLE)
        current = cur.key if cur is not None else None
        scroll = self.verticalScrollBar().value()
        sm = self.selectionModel()
        sm.blockSignals(True)
        self.model_.set_rows(self._rows, self._outgoing, self.threaded, self._grouping())
        expanded, collapsed = set(self._expanded), set(self._collapsed)
        for idx in self._walk():
            node = idx.data(NODE_ROLE)
            if node.kind == GROUP:
                self.setExpanded(idx, node.key not in collapsed)
            elif node.kind == THREAD:
                self.setExpanded(idx, node.key in expanded)
            if node.key == current:
                sm.setCurrentIndex(idx, sm.SelectionFlag.NoUpdate)
            if node.key in keys:
                sm.select(idx, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
        self._expanded, self._collapsed = expanded, collapsed
        self._apply_spans()
        sm.blockSignals(False)
        self.verticalScrollBar().setValue(scroll)

    # -- ids --------------------------------------------------------------
    def _node_ids(self, node: Node | None) -> list[str]:
        return [m["id"] for m in node.messages()] if node is not None else []

    def selected_ids(self) -> list[str]:
        """Messages in the selection; a selected conversation counts all of its messages."""
        out: list[str] = []
        seen: set[str] = set()
        for idx in self.selectionModel().selectedRows():
            for mid in self._node_ids(idx.data(NODE_ROLE)):
                if mid not in seen:
                    seen.add(mid)
                    out.append(mid)
        return out

    def selected_row_count(self) -> int:
        return len(self.selectionModel().selectedRows())

    def shown_id(self) -> str | None:
        """The message the reading pane should show: the one selected message, or the newest
        message of the one selected conversation."""
        rows = self.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        return self._display_id(rows[0].data(NODE_ROLE))

    def current_id(self) -> str | None:
        return self._display_id(self.currentIndex().data(NODE_ROLE))

    @staticmethod
    def _display_id(node: Node | None) -> str | None:
        if node is None or node.kind == GROUP:
            return None
        return node.row["newest_id"] if node.kind == THREAD else node.row["id"]

    # -- input ------------------------------------------------------------
    def _hit(self, event) -> str | None:
        """What a plain left click lands on: "read", "flag", "expand" or None."""
        if event.button() != Qt.MouseButton.LeftButton or event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            return None
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        node = idx.data(NODE_ROLE) if idx.isValid() else None
        if node is None:
            return None
        if node.kind == GROUP:
            return "expand"
        rect = self.visualRect(idx)
        if self.compact:
            if self._compact_delegate.dot_rect(rect, node).contains(pos):
                return "read"
            if node.kind == THREAD and self._compact_delegate.arrow_rect(rect, node).contains(pos):
                return "expand"
            if self._compact_delegate.flag_rect(rect).contains(pos):
                return "flag"
            return None
        key = COLUMNS[idx.column()].key
        if key == "subject" and node.kind == THREAD and \
                RowDelegate.arrow_rect(rect).contains(pos):
            return "expand"
        return {"read": "read", "flag": "flag"}.get(key)

    def _toggle_expanded(self, idx: QModelIndex):
        idx = idx.siblingAtColumn(0)
        self.setExpanded(idx, not self.isExpanded(idx))

    def mousePressEvent(self, event):
        target = self._hit(event)
        if target:
            idx = self.indexAt(event.position().toPoint())
            if target == "expand":
                self._toggle_expanded(idx)
                return
            node = idx.data(NODE_ROLE)
            # Toggle the clicked row without changing the selection (Thunderbird does the
            # same); within a multi-selection, toggle the whole selection.
            selected = {i.data(NODE_ROLE).key for i in self.selectionModel().selectedRows()}
            ids = self.selected_ids() if node.key in selected else self._node_ids(node)
            if target == "read":
                self.toggleReadRequested.emit(ids)
            else:
                self.toggleFlagRequested.emit(ids)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._hit(event):
            return  # two quick toggles, not an open
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        node = self.currentIndex().data(NODE_ROLE)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and node is not None:
            if node.kind == MESSAGE:
                self.openRequested.emit(node.row["id"])
            else:
                self._toggle_expanded(self.currentIndex())
            return
        super().keyPressEvent(event)

    def _on_double_clicked(self, idx):
        node = idx.data(NODE_ROLE)
        if node is None:
            return
        if node.kind == MESSAGE:
            self.openRequested.emit(node.row["id"])
        else:
            self._toggle_expanded(idx)


class FilterBar(QWidget):
    """Quick filter: Unread / Flagged / Attachment toggles plus one search box.
    Every word typed must match the sender, recipients, subject, preview or body."""
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
        self.search.setToolTip(
            "Every word must appear in the sender, recipients, subject or message text.\n"
            "Downloaded mail is searched at once; the server is searched too, to find mail "
            "that isn't stored on this computer. Esc clears.")
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

    def search_text(self) -> str:
        return "" if self.isHidden() else self.search.text().strip()

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

    def set_count(self, shown: int, note: str = ""):
        """`note` says how the server search went ("searching server…", ...)."""
        word = "match" if shown == 1 else "matches"
        if not self.active():
            word = "message" if shown == 1 else "messages"
        self.count.setText(f"{shown} {word}" + (f" · {note}" if note else ""))
        self.count.setToolTip(note)
