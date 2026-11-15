"""Choose a folder: for Move to folder / Copy to folder and Move folder.

One mailbox's folders in the folder pane's order, with a filter box: typing keeps the folders
whose name contains the text (and the folders above them). Enter or a double-click chooses.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..store import Store
from . import iconset
from .folder_pane import _ICONS, folder_order

ID_ROLE = Qt.ItemDataRole.UserRole
TOP = ""  # the top of the mailbox (only offered when moving a folder)


class FolderPicker(QDialog):
    def __init__(self, store: Store, mailbox_id: str, title: str, prompt: str,
                 disabled: frozenset[str] = frozenset(), subtree: str | None = None,
                 allow_top: bool = False, preselect: str | None = None, parent=None):
        """`disabled` folders can't be chosen; nor can `subtree` and anything below it (a
        folder can't move into itself). `allow_top` offers the top of the mailbox (TOP)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(380, 480)
        lay = QVBoxLayout(self)
        label = QLabel(prompt)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Type to find a folder")
        self.filter.setClearButtonEnabled(True)
        self.filter.addAction(iconset.load("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.itemDoubleClicked.connect(lambda item, _: self._choose(item))
        lay.addWidget(self.tree, 1)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(lambda: self._choose(self.tree.currentItem()))
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self._ok = box.button(QDialogButtonBox.StandardButton.Ok)
        self.tree.currentItemChanged.connect(lambda cur, _: self._ok.setEnabled(
            cur is not None and bool(cur.flags() & Qt.ItemFlag.ItemIsSelectable)))
        self.chosen: str | None = None

        mailbox = next((m for m in store.mailbox_rows() if m["id"] == mailbox_id), None)
        root = QTreeWidgetItem([mailbox["display_name"] if mailbox else "Mailbox"])
        root.setIcon(0, iconset.load("users-round" if mailbox and mailbox["address"]
                                     else "user-round"))
        root.setData(0, ID_ROLE, TOP)
        if not allow_top:
            root.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.tree.addTopLevelItem(root)

        rows = store.folder_rows(mailbox_id)
        ids = {r["id"] for r in rows}
        kids: dict[str | None, list] = {}
        for r in rows:
            kids.setdefault(r["parent_id"] if r["parent_id"] in ids else None, []).append(r)
        items: dict[str, QTreeWidgetItem] = {}

        def add(parent: QTreeWidgetItem, parent_id: str | None, blocked: bool):
            entries = kids.get(parent_id, [])
            entries = folder_order(entries) if parent_id is None else sorted(
                entries, key=lambda r: r["name"].casefold())
            for r in entries:
                item = QTreeWidgetItem([r["name"]])
                item.setIcon(0, iconset.load(_ICONS.get(r["well_known"], "folder")))
                item.setData(0, ID_ROLE, r["id"])
                inside = blocked or r["id"] == subtree
                if inside or r["id"] in disabled:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                parent.addChild(item)
                items[r["id"]] = item
                add(item, r["id"], inside)

        add(root, None, False)
        self.tree.expandAll()
        start = items.get(preselect) if preselect else None
        if start is not None and start.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.tree.setCurrentItem(start)
        else:
            self.tree.setCurrentItem(root)
            self._ok.setEnabled(allow_top)
        self.filter.setFocus()

    def _apply_filter(self, text: str):
        text = text.strip().casefold()

        def visit(item: QTreeWidgetItem) -> bool:
            """Show items that match, or have a match below them."""
            below = False
            for i in range(item.childCount()):
                below = visit(item.child(i)) or below
            match = not text or text in item.text(0).casefold()
            item.setHidden(not (match or below))
            return match or below

        visit(self.tree.topLevelItem(0))
        self.tree.topLevelItem(0).setHidden(False)
        if text:  # jump to the first selectable match
            it = self.tree.topLevelItem(0)
            stack = [it]
            while stack:
                it = stack.pop(0)
                if (not it.isHidden() and it.flags() & Qt.ItemFlag.ItemIsSelectable
                        and text in it.text(0).casefold()):
                    self.tree.setCurrentItem(it)
                    break
                stack.extend(it.child(i) for i in range(it.childCount()))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._choose(self.tree.currentItem())
            return
        super().keyPressEvent(event)

    def _choose(self, item: QTreeWidgetItem | None):
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsSelectable:
            return
        self.chosen = item.data(0, ID_ROLE)
        self.accept()
