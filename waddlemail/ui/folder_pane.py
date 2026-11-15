"""Folder pane: one top-level node per mailbox, its folders below in Outlook order.

Default folders (Inbox, Drafts, Sent Items, ...) come first in a fixed order, then the rest A-Z;
subfolders are A-Z. Each row shows an icon for its folder type and a right-aligned count: unread
mail (accent colour), or the number of items for Drafts and Outbox. A collapsed folder also counts
the unread mail in the subfolders it hides, in a dimmer colour.

New folder and Rename edit the name in place (as in Outlook); the pane only asks, through
signals, and MainWindow does the work on the server and repopulates.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QMenu, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem,
)

from ..graph import WELL_KNOWN
from ..store import Store
from . import iconset

ID_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1     # "mailbox" or "folder"
COUNT_ROLE = Qt.ItemDataRole.UserRole + 2    # own badge number (0 = none)
TOTAL_ROLE = Qt.ItemDataRole.UserRole + 3    # badge counts items, not unread (Drafts, Outbox)
HIDDEN_ROLE = Qt.ItemDataRole.UserRole + 4   # unread mail in descendants
NOTE_ROLE = Qt.ItemDataRole.UserRole + 5     # dim text after the name ("Shared")
WELL_KNOWN_ROLE = Qt.ItemDataRole.UserRole + 6

_ICONS = {
    "inbox": "inbox", "drafts": "file-pen-line", "sentitems": "send",
    "deleteditems": "trash-2", "junkemail": "shield-alert", "archive": "archive",
    "outbox": "upload",
}
_COUNT_TOTAL = {"drafts", "outbox"}


def folder_order(rows) -> list:
    """Default folders first (in WELL_KNOWN order), then the rest A-Z."""
    rank = {name: i for i, name in enumerate(WELL_KNOWN)}
    return sorted(rows, key=lambda r: (rank.get(r["well_known"], len(rank)),
                                       r["name"].casefold()))


def _contrast(a: QColor, b: QColor) -> float:
    def lum(c: QColor) -> float:
        def ch(v: float) -> float:
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _accent(pal: QPalette, group) -> QColor:
    """The theme's selection colour for unread counts, unless it is hard to read on the
    background; then the link colour."""
    base = pal.color(group, QPalette.ColorRole.Base)
    highlight = pal.color(group, QPalette.ColorRole.Highlight)
    if _contrast(highlight, base) >= 3:
        return highlight
    return pal.color(group, QPalette.ColorRole.Link)


class FolderDelegate(QStyledItemDelegate):
    edited = Signal(str)  # the text typed when an in-place edit is committed

    def setModelData(self, editor, model, index):
        # Don't write the name into the item: the pane decides (and the server may refuse).
        self.edited.emit(editor.text())

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), option.fontMetrics.height() + 10))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        name, opt.text = opt.text, ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        rect = rect.adjusted(2, 0, -6, 0)
        pal = opt.palette
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        group = (QPalette.ColorGroup.Active if opt.state & QStyle.StateFlag.State_Enabled
                 else QPalette.ColorGroup.Disabled)
        text_color = pal.color(group, QPalette.ColorRole.HighlightedText if selected
                               else QPalette.ColorRole.Text)
        dim_color = text_color if selected else pal.color(group, QPalette.ColorRole.PlaceholderText)
        accent = text_color if selected else _accent(pal, group)

        badge, color, bold = self._badge(index, widget, accent, dim_color)
        painter.save()
        if badge:
            bfont = QFont(opt.font)
            bfont.setBold(bold)
            fm = QFontMetrics(bfont)
            w = fm.horizontalAdvance(badge)
            painter.setFont(bfont)
            painter.setPen(color)
            painter.drawText(QRect(rect.right() - w, rect.top(), w, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, badge)
            rect.setRight(rect.right() - w - 8)

        fm = QFontMetrics(opt.font)
        note = index.data(NOTE_ROLE) or ""
        note_w = 0
        if note:
            nfont = QFont(opt.font)
            nfont.setBold(False)
            note_w = QFontMetrics(nfont).horizontalAdvance(note) + 6
        text = fm.elidedText(name, Qt.TextElideMode.ElideRight, max(rect.width() - note_w, 0))
        painter.setFont(opt.font)
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if note and text == name:
            painter.setFont(nfont)
            painter.setPen(dim_color)
            painter.drawText(rect.adjusted(fm.horizontalAdvance(text) + 6, 0, 0, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, note)
        painter.restore()

    @staticmethod
    def _badge(index, widget, accent, dim):
        """(text, colour, bold) for the right-hand count."""
        own = index.data(COUNT_ROLE) or 0
        hidden = index.data(HIDDEN_ROLE) or 0
        collapsed = widget is not None and not widget.isExpanded(index)
        if index.data(TOTAL_ROLE):
            return (str(own), dim, False) if own else ("", None, False)
        if collapsed and hidden:
            return str(own + hidden), (accent if own else dim), bool(own)
        return (str(own), accent, True) if own else ("", None, False)


class FolderPane(QTreeWidget):
    folderSelected = Signal(str)
    removeMailboxRequested = Signal(str)
    createFolderRequested = Signal(str, str, str)  # mailbox id, parent folder id ("" = top), name
    renameFolderRequested = Signal(str, str)       # folder id, new name
    deleteFolderRequested = Signal(str)            # folder id

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self._extra_actions: list[QAction] = []
        self.setHeaderHidden(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIndentation(14)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(16, 16))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._delegate = FolderDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)  # F2 / menu only
        self._edit: dict | None = None     # the in-place edit in progress
        self._populate_later = False       # a refresh that arrived while editing
        self._delegate.edited.connect(self._on_edited)
        self._delegate.closeEditor.connect(lambda *_: QTimer.singleShot(0, self._edit_closed))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.itemSelectionChanged.connect(self._on_selection)
        self.itemClicked.connect(self._on_clicked)
        self.itemCollapsed.connect(lambda it: self._on_toggled(it, True))
        self.itemExpanded.connect(lambda it: self._on_toggled(it, False))

    def set_context_actions(self, actions: list[QAction]) -> None:
        """Actions offered on every right-click (Add account, Add shared mailbox, ...)."""
        self._extra_actions = actions

    # -- building -------------------------------------------------------
    def populate(self) -> None:
        if self._edit is not None:  # rebuilding would destroy the editor: do it afterwards
            self._populate_later = True
            return
        self._populate_later = False
        current = self.current_folder()
        scroll = self.verticalScrollBar().value()
        self.blockSignals(True)
        self.clear()
        for mb in self.store.mailbox_rows():
            root = QTreeWidgetItem([mb["display_name"]])
            root.setData(0, ID_ROLE, mb["id"])
            root.setData(0, KIND_ROLE, "mailbox")
            root.setIcon(0, iconset.load("users-round" if mb["address"] else "user-round"))
            root.setToolTip(0, mb["address"] or mb["display_name"])
            if mb["address"]:
                root.setData(0, NOTE_ROLE, "Shared")
            font = QFont(self.font())
            font.setBold(True)
            root.setFont(0, font)
            root.setFlags(Qt.ItemFlag.ItemIsEnabled)  # a heading, not a folder
            self.addTopLevelItem(root)
            unread = self._add_folders(root, self.store.folder_rows(mb["id"]))
            root.setData(0, HIDDEN_ROLE, unread)
            root.setExpanded(not mb["collapsed"])
        self.blockSignals(False)
        if current:
            self.select_folder(current, emit=False)
        self.verticalScrollBar().setValue(scroll)

    def _add_folders(self, root: QTreeWidgetItem, rows) -> int:
        """Build one mailbox's subtree; returns its total unread."""
        ids = {r["id"] for r in rows}
        kids: dict[str | None, list] = {}
        for r in rows:  # folders whose parent we can't see (top of mailbox, shared) go on top
            kids.setdefault(r["parent_id"] if r["parent_id"] in ids else None, []).append(r)

        def add(parent: QTreeWidgetItem, parent_id: str | None) -> int:
            entries = kids.get(parent_id, [])
            entries = folder_order(entries) if parent_id is None else sorted(
                entries, key=lambda r: r["name"].casefold())
            total = 0
            for r in entries:
                item = self._folder_item(r)
                parent.addChild(item)
                below = add(item, r["id"])
                item.setData(0, HIDDEN_ROLE, below)
                item.setExpanded(not r["collapsed"])
                self._update_folder_icon(item)
                total += (r["unread"] or 0) + below
            return total

        return add(root, None)

    def _folder_item(self, r) -> QTreeWidgetItem:
        item = QTreeWidgetItem([r["name"]])
        item.setData(0, ID_ROLE, r["id"])
        item.setData(0, KIND_ROLE, "folder")
        wk = r["well_known"]
        item.setData(0, WELL_KNOWN_ROLE, wk)
        item.setIcon(0, iconset.load(_ICONS.get(wk, "folder")))
        self._set_counts(item, r)
        return item

    def _set_counts(self, item: QTreeWidgetItem, r) -> None:
        wk = r["well_known"]
        total_only = wk in _COUNT_TOTAL
        item.setData(0, TOTAL_ROLE, total_only)
        item.setData(0, COUNT_ROLE, (r["total"] if total_only else r["unread"]) or 0)
        font = QFont(self.font())
        font.setBold(bool(r["unread"]) and not total_only)
        item.setFont(0, font)
        tip = f"{r['total']} items, {r['unread']} unread" if r["total"] else "Empty"
        item.setToolTip(0, f"{r['name']}\n{tip}")

    def refresh_counts(self) -> None:
        """Re-read unread/total counts from the store without rebuilding the tree."""
        rows = {r["id"]: r for r in self.store.folder_rows()}

        def walk(item: QTreeWidgetItem) -> int:
            below = 0
            for i in range(item.childCount()):
                child = item.child(i)
                r = rows.get(child.data(0, ID_ROLE))
                if r is not None:
                    self._set_counts(child, r)
                below += ((r["unread"] or 0) if r is not None else 0) + walk(child)
            item.setData(0, HIDDEN_ROLE, below)
            return below

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))

    # -- lookup / selection ----------------------------------------------
    def current_folder(self) -> str | None:
        item = self.currentItem()
        if item is not None and item.data(0, KIND_ROLE) == "folder":
            return item.data(0, ID_ROLE)
        return None

    def _find(self, item_id: str) -> QTreeWidgetItem | None:
        stack = [self.invisibleRootItem()]
        while stack:
            node = stack.pop()
            for i in range(node.childCount()):
                child = node.child(i)
                if child.data(0, ID_ROLE) == item_id:
                    return child
                stack.append(child)
        return None

    def select_folder(self, folder_id: str, emit: bool = True) -> None:
        item = self._find(folder_id)
        if item is None:
            return
        parent = item.parent()
        while parent is not None:  # reveal it
            parent.setExpanded(True)
            parent = parent.parent()
        if not emit:
            self.blockSignals(True)
        self.setCurrentItem(item)
        self.blockSignals(False)
        self.scrollToItem(item)

    def _on_selection(self):
        fid = self.current_folder()
        if fid:
            self.folderSelected.emit(fid)

    def _on_clicked(self, item: QTreeWidgetItem, _col: int):
        if item.data(0, KIND_ROLE) == "mailbox":
            item.setExpanded(not item.isExpanded())

    def _on_toggled(self, item: QTreeWidgetItem, collapsed: bool):
        if item.data(0, KIND_ROLE) == "mailbox":
            self.store.set_mailbox_collapsed(item.data(0, ID_ROLE), collapsed)
        else:
            self.store.set_folder_collapsed(item.data(0, ID_ROLE), collapsed)
            self._update_folder_icon(item)

    @staticmethod
    def _update_folder_icon(item: QTreeWidgetItem):
        """Plain folders show an open folder while their subfolders are showing."""
        if item.data(0, WELL_KNOWN_ROLE) in _ICONS:
            return
        is_open = item.isExpanded() and item.childCount() > 0
        item.setIcon(0, iconset.load("folder-open" if is_open else "folder"))

    # -- new / rename / delete ---------------------------------------------
    @staticmethod
    def _mailbox_of(item: QTreeWidgetItem) -> QTreeWidgetItem:
        while item.parent() is not None:
            item = item.parent()
        return item

    def start_new_folder(self, parent: QTreeWidgetItem | None = None) -> None:
        """Add a "New folder" row under `parent` (a folder or a mailbox heading; default: the
        current folder) and let the user type its name."""
        parent = parent or self.currentItem()
        if parent is None or self._edit is not None:
            return
        item = QTreeWidgetItem(["New folder"])
        item.setIcon(0, iconset.load("folder"))
        item.setData(0, KIND_ROLE, "new")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
        parent.addChild(item)
        parent.setExpanded(True)
        mailbox = self._mailbox_of(parent)
        self._edit = {"mode": "new", "item": item, "mailbox": mailbox.data(0, ID_ROLE),
                      "parent": parent.data(0, ID_ROLE)
                      if parent.data(0, KIND_ROLE) == "folder" else "", "text": None}
        self.scrollToItem(item)
        self.editItem(item, 0)

    def start_rename(self, item: QTreeWidgetItem | None = None) -> None:
        item = item or self.currentItem()
        if item is None or self._edit is not None or not self.can_change(item):
            return
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._edit = {"mode": "rename", "item": item, "text": None}
        self.editItem(item, 0)

    def request_delete(self, item: QTreeWidgetItem | None = None) -> None:
        item = item or self.currentItem()
        if item is not None and self.can_change(item):
            self.deleteFolderRequested.emit(item.data(0, ID_ROLE))

    @staticmethod
    def can_change(item: QTreeWidgetItem) -> bool:
        """Default folders (Inbox, Sent Items, ...) can't be renamed or deleted."""
        return item.data(0, KIND_ROLE) == "folder" and not item.data(0, WELL_KNOWN_ROLE)

    def _on_edited(self, text: str):
        if self._edit is not None:
            self._edit["text"] = text.strip()

    def _edit_closed(self):
        edit, self._edit = self._edit, None
        if edit is None:
            return
        item, text = edit["item"], edit["text"]
        if edit["mode"] == "new":
            if text:
                item.setText(0, text)  # shown until the refresh after the server answers
                self.createFolderRequested.emit(edit["mailbox"], edit["parent"], text)
            else:  # Esc, or nothing typed
                item.parent().removeChild(item)
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if text and text != item.text(0):
                item.setText(0, text)
                self.renameFolderRequested.emit(item.data(0, ID_ROLE), text)
        if self._populate_later:
            self.populate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F2:
            self.start_rename()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.request_delete()
            return
        super().keyPressEvent(event)

    # -- context menu ------------------------------------------------------
    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        menu = QMenu(self)
        if item is not None and item.data(0, KIND_ROLE) in ("folder", "mailbox"):
            where = "&New folder…" if item.data(0, KIND_ROLE) == "mailbox" else "&New subfolder…"
            menu.addAction(iconset.load("folder-plus"), where,
                           lambda: self.start_new_folder(item))
            if item.data(0, KIND_ROLE) == "folder":
                changeable = self.can_change(item)
                act = menu.addAction(iconset.load("pencil"), "&Rename\tF2",
                                     lambda: self.start_rename(item))
                act.setEnabled(changeable)
                act = menu.addAction(iconset.load("trash-2"), "&Delete\tDel",
                                     lambda: self.request_delete(item))
                act.setEnabled(changeable)
            menu.addSeparator()
        root = self._mailbox_of(item) if item is not None else None
        if root is not None:
            shared = bool(root.data(0, NOTE_ROLE))
            label = "&Remove shared mailbox" if shared else "Sign &out of this account"
            mid = root.data(0, ID_ROLE)
            menu.addAction(label, lambda: self.removeMailboxRequested.emit(mid))
            menu.addSeparator()
        for act in self._extra_actions:
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("&Expand all", lambda: self._set_all_expanded(True))
        menu.addAction("Co&llapse all", lambda: self._set_all_expanded(False))
        menu.exec(event.globalPos())

    def _set_all_expanded(self, expanded: bool):
        """Expand/collapse every folder (remembered); mailbox headings stay open.
        Item by item, because QTreeView.expandAll() doesn't emit the signals we persist with."""
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, KIND_ROLE) == "folder" and item.childCount():
                item.setExpanded(expanded)
            elif item.data(0, KIND_ROLE) == "mailbox":
                item.setExpanded(True)
            stack.extend(item.child(i) for i in range(item.childCount()))
