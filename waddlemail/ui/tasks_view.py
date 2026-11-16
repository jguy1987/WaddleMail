"""The Tasks page, as Outlook 2010's To-Do List: your To Do lists on the left (with how many
are open, and overdue), and the tasks grouped by when they're due: Overdue, Today, Tomorrow,
This week, Next week, Later, No date (and Completed, if shown). Overdue tasks are red.

Type a task in the box at the top and press Enter to add it, due today. Tick one to mark it
complete. Click a group's heading to fold it (Completed starts folded). Double-click (or Enter) opens it; right-click for the follow-up dates (Today,
Tomorrow, This week, Next week, No date), Open email (for one made from a message), Delete.
The page only shows and asks: MainWindow does the work (the signals)."""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QLineEdit, QMenu, QSplitter, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..calendars import describe_recurrence
from ..store import Store
from ..tasks import GROUPS, QUICK, due_group
from . import iconset
from .event_editor import color_icon

ID_ROLE = Qt.ItemDataRole.UserRole
GROUP_ROLE = Qt.ItemDataRole.UserRole + 1  # a heading's group name
OVERDUE = "#c42b1c"
ALL = ""  # the "To-Do List" item: every list's open tasks
COLUMNS = ["", "!", "Task", "Due", "List", "Categories"]


class TasksPage(QWidget):
    newRequested = Signal(str)               # list id (ALL: the default list)
    quickAddRequested = Signal(str, str)     # list id, title
    editRequested = Signal(str)              # task id
    completeRequested = Signal(list, bool)   # task ids, complete?
    dueRequested = Signal(list, str)         # task ids, a QUICK key
    deleteRequested = Signal(list)           # task ids
    openEmailRequested = Signal(str)         # task id
    refreshRequested = Signal()

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self._collapsed: set[str] = {"Completed"}
        self._filling = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter()
        lay.addWidget(split)

        self.lists = QTreeWidget()
        self.lists.setHeaderHidden(True)
        self.lists.setRootIsDecorated(False)
        self.lists.currentItemChanged.connect(lambda *_: self.reload_tasks())
        split.addWidget(self.lists)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(6, 6, 6, 0)
        top = QHBoxLayout()
        self.quick_add = QLineEdit()
        self.quick_add.setPlaceholderText("Type a new task (due today) and press Enter")
        self.quick_add.returnPressed.connect(self._quick_add)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tasks")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self.reload_tasks())
        self.show_completed = QCheckBox("Show completed")
        self.show_completed.toggled.connect(lambda _: self.reload_tasks())
        refresh = QToolButton()
        refresh.setIcon(iconset.load("refresh-cw"))
        refresh.setToolTip("Get tasks from the server")
        refresh.clicked.connect(self.refreshRequested)
        top.addWidget(self.quick_add, 2)
        top.addWidget(self.search, 1)
        top.addWidget(self.show_completed)
        top.addWidget(refresh)
        rlay.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        for col, width in enumerate((30, 24, 380, 110, 120)):
            self.tree.setColumnWidth(col, width)
        self.tree.itemChanged.connect(self._ticked)
        self.tree.itemDoubleClicked.connect(self._activated)
        self.tree.itemClicked.connect(self._heading_clicked)
        self.tree.itemExpanded.connect(lambda i: self._collapsed.discard(i.data(0, GROUP_ROLE)))
        self.tree.itemCollapsed.connect(lambda i: self._collapsed.add(i.data(0, GROUP_ROLE)))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        for key, slot in ((QKeySequence(Qt.Key.Key_Return), self._open_current),
                          (QKeySequence(Qt.Key.Key_Enter), self._open_current),
                          (QKeySequence.StandardKey.Delete,
                           lambda: self.selected() and self.deleteRequested.emit(self.selected()))):
            sc = QShortcut(key, self.tree)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)
        rlay.addWidget(self.tree, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 900])

    # -- filling in --------------------------------------------------------------------------
    def current_list(self) -> str:
        item = self.lists.currentItem()
        return item.data(0, ID_ROLE) if item is not None else ALL

    def reload(self):
        """Lists (with counts), then tasks."""
        keep = self.current_list()
        today = dt.date.today().isoformat()
        counts = self.store.task_counts(today)
        rows = self.store.task_lists()
        accounts = {r["account_id"] for r in rows}
        self.lists.blockSignals(True)
        self.lists.clear()
        total = sum(n for n, _ in counts.values())
        overdue = sum(o or 0 for _, o in counts.values())
        everything = QTreeWidgetItem([self._label("To-Do List", total, overdue)])
        everything.setData(0, ID_ROLE, ALL)
        everything.setIcon(0, iconset.load("square-check-big"))
        self.lists.addTopLevelItem(everything)
        current = everything
        for r in rows:
            n, late = counts.get(r["id"], (0, 0))
            name = r["name"] if len(accounts) == 1 else f"{r['name']} · {r['account_id']}"
            item = QTreeWidgetItem([self._label(name, n, late or 0)])
            item.setData(0, ID_ROLE, r["id"])
            item.setIcon(0, iconset.load("flag" if r["well_known"] == "flaggedEmails" else
                                         "list"))
            self.lists.addTopLevelItem(item)
            if r["id"] == keep:
                current = item
        self.lists.setCurrentItem(current)
        self.lists.blockSignals(False)
        self.reload_tasks()

    @staticmethod
    def _label(name: str, n: int, overdue: int) -> str:
        if not n:
            return name
        return f"{name}  {n}" + (f" ({overdue} overdue)" if overdue else "")

    def overdue_count(self) -> int:
        return sum(o or 0 for _, o in self.store.task_counts(dt.date.today().isoformat())
                   .values())

    def reload_tasks(self):
        """The current list's tasks, grouped by when they're due (selection kept)."""
        keep, current = set(self.selected()), self.current_id()
        lid = self.current_list()
        lists = {r["id"]: r for r in self.store.task_lists()}
        rows = self.store.tasks([lid] if lid else None, completed=self.show_completed.isChecked(),
                                search=self.search.text())
        colors = self.store.category_colors()
        today = dt.date.today()
        groups: dict[str, list] = {}
        for r in rows:
            due = dt.date.fromisoformat(r["due"]) if r["due"] else None
            groups.setdefault(due_group(due, today, r["status"] == "completed"), []).append(r)
        self._filling = True
        self.tree.clear()
        for name in GROUPS:
            if name not in groups:
                continue
            n = len(groups[name])
            head = QTreeWidgetItem([f"{name}  ({n})"])
            head.setData(0, GROUP_ROLE, name)
            head.setFirstColumnSpanned(True)
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = QFont(self.tree.font())
            font.setBold(True)
            head.setFont(0, font)
            if name == "Overdue":
                head.setForeground(0, QBrush(QColor(OVERDUE)))
            head.setToolTip(0, "Click to fold or unfold")
            self.tree.addTopLevelItem(head)
            for r in groups[name]:
                head.addChild(self._item(r, name == "Overdue", lists, colors))
            head.setExpanded(name not in self._collapsed)
            # (the span is per row: set again once it's in the tree)
            head.setFirstColumnSpanned(True)
        self._filling = False
        for item in self._task_items():
            if item.data(0, ID_ROLE) in keep:
                item.setSelected(True)
            if item.data(0, ID_ROLE) == current:
                self.tree.setCurrentItem(item, 0,
                                         self.tree.selectionModel().SelectionFlag.NoUpdate)

    def _item(self, r, overdue: bool, lists: dict, colors: dict) -> QTreeWidgetItem:
        due = dt.date.fromisoformat(r["due"]) if r["due"] else None
        cats = json.loads(r["categories"] or "[]")
        lst = lists.get(r["list_id"])
        item = QTreeWidgetItem(["", "", r["title"] or "(no subject)",
                                f"{due:%a} {due.day} {due:%b}" if due else "",
                                lst["name"] if lst else "", ", ".join(cats)])
        item.setData(0, ID_ROLE, r["id"])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                      | Qt.ItemFlag.ItemIsUserCheckable)
        done = r["status"] == "completed"
        item.setCheckState(0, Qt.CheckState.Checked if done else Qt.CheckState.Unchecked)
        item.setToolTip(0, "Mark not complete" if done else "Mark complete")
        if r["importance"] == "high":
            item.setText(1, "!")
            item.setForeground(1, QBrush(QColor(OVERDUE)))
            item.setToolTip(1, "High priority")
        elif r["importance"] == "low":
            item.setText(1, "↓")
            item.setToolTip(1, "Low priority")
        if r["reminder"]:
            item.setIcon(2, iconset.load("bell"))
        elif self._has_email(r):
            item.setIcon(2, iconset.load("mail"))
        if r["recurrence"]:
            item.setIcon(3, iconset.load("repeat"))
            item.setToolTip(3, describe_recurrence(json.loads(r["recurrence"])))
        if cats:
            item.setIcon(5, color_icon(colors.get(cats[0], "#a0a0a0")))
        tip = [r["title"] or ""]
        if r["body"]:
            tip.append(r["body"][:300])
        item.setToolTip(2, "\n\n".join(tip))
        if overdue:
            for col in (2, 3):
                item.setForeground(col, QBrush(QColor(OVERDUE)))
        if done:
            font = QFont(self.tree.font())
            font.setStrikeOut(True)
            grey = self.palette().color(QPalette.ColorRole.PlaceholderText)
            for col in range(len(COLUMNS)):
                item.setFont(col, font)
                item.setForeground(col, QBrush(grey))
        return item

    def _task_items(self) -> list[QTreeWidgetItem]:
        return [self.tree.topLevelItem(i).child(j) for i in range(self.tree.topLevelItemCount())
                for j in range(self.tree.topLevelItem(i).childCount())]

    def selected(self) -> list[str]:
        return [i.data(0, ID_ROLE) for i in self.tree.selectedItems() if i.data(0, ID_ROLE)]

    def current_id(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, ID_ROLE) if item is not None else None

    # -- doing -------------------------------------------------------------------------------
    def _quick_add(self):
        title = self.quick_add.text().strip()
        if title:
            self.quickAddRequested.emit(self.current_list(), title)
            self.quick_add.clear()

    def _ticked(self, item: QTreeWidgetItem, column: int):
        if self._filling or column != 0 or not item.data(0, ID_ROLE):
            return
        # After this event: completing reloads the list, which would delete the item that's
        # still sending the signal.
        ids, done = [item.data(0, ID_ROLE)], item.checkState(0) == Qt.CheckState.Checked
        QTimer.singleShot(0, lambda: self.completeRequested.emit(ids, done))

    def _heading_clicked(self, item: QTreeWidgetItem, _column: int):
        if item.data(0, GROUP_ROLE):
            item.setExpanded(not item.isExpanded())

    def _activated(self, item: QTreeWidgetItem, column: int):
        if item.data(0, ID_ROLE) and column != 0:
            self.editRequested.emit(item.data(0, ID_ROLE))

    def _open_current(self):
        if self.current_id():
            self.editRequested.emit(self.current_id())

    def _menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is not None and item.data(0, ID_ROLE) and not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        ids = self.selected()
        menu = QMenu(self)
        if ids:
            menu.addAction("&Open", lambda: self.editRequested.emit(ids[0])).setEnabled(
                len(ids) == 1)
            rows = [self.store.task(i) for i in ids]
            done = all(r is not None and r["status"] == "completed" for r in rows)
            menu.addAction(iconset.load("check"),
                           "Mark &not complete" if done else "Mark &complete",
                           lambda: self.completeRequested.emit(ids, not done))
            follow = menu.addMenu(iconset.load("flag"), "&Follow up")
            for key, label in QUICK:
                follow.addAction(label, lambda k=key: self.dueRequested.emit(ids, k))
            if len(ids) == 1 and self._has_email(rows[0]):
                menu.addAction(iconset.load("mail"), "Open &email",
                               lambda: self.openEmailRequested.emit(ids[0]))
            menu.addSeparator()
            menu.addAction(iconset.load("trash-2"), "&Delete\tDel",
                           lambda: self.deleteRequested.emit(ids))
            menu.addSeparator()
        menu.addAction(iconset.load("square-check-big"), "New &task…",
                       lambda: self.newRequested.emit(self.current_list()))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _has_email(self, row) -> bool:
        if row is None:
            return False
        lst = self.store.task_list(row["list_id"])
        return (lst is not None and lst["well_known"] == "flaggedEmails") or \
            row["links"] not in (None, "[]")
