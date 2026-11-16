"""Reminders, as Outlook does them: a window that stays on top listing what is due, with
Join Teams / Open / Dismiss / Dismiss all / Snooze, and a desktop notification when a reminder
goes off.

Reminders are worked out from the events cached for the coming week (calendars.due_reminders)
and the cached tasks (tasks.due_reminders), so they also work offline. Dismissing and snoozing
is remembered locally and sent to Exchange / To Do (MainWindow), so other devices agree. A
task reminds once (when its reminder goes off); besides that there's one summary a day of the
tasks overdue and due today.

Reminder keys: (event id, start) for an event's occurrence; ("task:" + task id, reminder time)
for a task.
"""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from ..calendars import due_reminders, event_span, period
from ..store import Store
from ..tasks import due_reminders as due_tasks
from ..tasks import due_text as task_due_text
from . import iconset
from .calendar_view import fmt_time

KEY_ROLE = Qt.ItemDataRole.UserRole
SUMMARY_HOUR = 8  # the day's task summary: at the first check after 8:00


def is_task(row) -> bool:
    return "list_id" in row.keys()  # noqa: SIM118 (a sqlite3.Row's `in` looks at values)


def reminder_key(row) -> tuple[str, str]:
    return (f"task:{row['id']}", row["reminder"]) if is_task(row) else (row["id"], row["start"])


def _task_due(row, now: dt.datetime) -> str:
    return task_due_text(dt.date.fromisoformat(row["due"]) if row["due"] else None, now.date())
SNOOZE = [(5, "5 minutes"), (10, "10 minutes"), (15, "15 minutes"), (30, "30 minutes"),
          (60, "1 hour"), (120, "2 hours"), (240, "4 hours"), (1440, "1 day")]


def due_text(row, now: dt.datetime) -> str:
    start, end = event_span(row)
    minutes = round((start - now).total_seconds() / 60)
    if minutes > 0:
        if minutes < 60:
            return f"in {minutes} min"
        if minutes < 24 * 60:
            return f"in {minutes // 60} h {minutes % 60:02d} min"
        return f"in {minutes // 1440} day(s)"
    if now < end:
        return "Now" if minutes > -1 else f"started {-minutes} min ago"
    return "Ended"


class ReminderWindow(QDialog):
    dismissRequested = Signal(list)       # [(event id, start), ...]
    snoozeRequested = Signal(list, int)   # keys, minutes
    openRequested = Signal(str)           # event id
    joinRequested = Signal(str)           # Teams link

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reminders")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowIcon(iconset.load("calendar"))
        self.resize(520, 300)
        lay = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        lay.addWidget(self.heading)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Subject", "Due"])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setColumnWidth(0, 340)
        self.list.itemSelectionChanged.connect(self._selection)
        self.list.itemDoubleClicked.connect(lambda *_: self._open())
        lay.addWidget(self.list, 1)
        top = QHBoxLayout()
        self.join = QPushButton(iconset.load("video"), "Join Teams")
        self.join.clicked.connect(self._join)
        self.open_b = QPushButton("Open item")
        self.open_b.clicked.connect(self._open)
        self.dismiss = QPushButton("Dismiss")
        self.dismiss.clicked.connect(lambda: self.dismissRequested.emit(self.selected_keys()))
        dismiss_all = QPushButton("Dismiss all")
        dismiss_all.clicked.connect(lambda: self.dismissRequested.emit(self.all_keys()))
        for b in (self.join, self.open_b):
            top.addWidget(b)
        top.addStretch()
        top.addWidget(self.dismiss)
        top.addWidget(dismiss_all)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Remind me again in:"))
        self.snooze_for = QComboBox()
        for minutes, label in SNOOZE:
            self.snooze_for.addItem(label, minutes)
        bottom.addWidget(self.snooze_for, 1)
        self.snooze = QPushButton("Snooze")
        self.snooze.clicked.connect(lambda: self.snoozeRequested.emit(
            self.selected_keys(), self.snooze_for.currentData()))
        bottom.addWidget(self.snooze)
        lay.addLayout(bottom)
        self._rows: dict = {}

    def set_reminders(self, rows, now: dt.datetime):
        """`rows`: events and tasks whose reminders are due."""
        keep = set(self.selected_keys())
        self._rows = {reminder_key(r): r for r in rows}
        self.list.clear()
        for r in rows:
            key = reminder_key(r)
            if is_task(r):
                item = QTreeWidgetItem([r["title"] or "(no subject)", _task_due(r, now)])
                item.setToolTip(0, (r["body"] or "")[:300])
                item.setIcon(0, iconset.load("square-check-big"))
            else:
                start, _ = event_span(r)
                when = "All day" if r["all_day"] else fmt_time(start)
                item = QTreeWidgetItem([r["subject"] or "(no title)", due_text(r, now)])
                item.setToolTip(0, f"{when}" + (f" · {r['location']}" if r["location"] else ""))
                item.setIcon(0, iconset.load("video" if r["online_url"] else "calendar"))
            item.setData(0, KEY_ROLE, key)
            self.list.addTopLevelItem(item)
            if key in keep:
                item.setSelected(True)
        if not self.list.selectedItems() and self.list.topLevelItemCount():
            first = self.list.topLevelItem(0)
            self.list.setCurrentItem(first)
            first.setSelected(True)
        n = len(rows)
        self.heading.setText(f"<b>{n} reminder{'s' if n != 1 else ''}</b>")
        self._selection()

    def selected_keys(self) -> list:
        return [i.data(0, KEY_ROLE) for i in self.list.selectedItems()]

    def all_keys(self) -> list:
        return list(self._rows)

    def _selected_row(self):
        keys = self.selected_keys()
        return self._rows.get(keys[0]) if len(keys) == 1 else None

    def _selection(self):
        row = self._selected_row()
        self.join.setEnabled(bool(row and not is_task(row) and row["online_url"]))
        self.open_b.setEnabled(row is not None)
        self.dismiss.setEnabled(bool(self.selected_keys()))
        self.snooze.setEnabled(bool(self.selected_keys()))

    def _open(self):
        keys = self.selected_keys()
        if len(keys) == 1:
            self.openRequested.emit(keys[0][0])  # (an event id, or "task:" + task id)

    def _join(self):
        row = self._selected_row()
        if row and not is_task(row) and row["online_url"]:
            self.joinRequested.emit(row["online_url"])


class ReminderService(QObject):
    """Checks every half minute; shows the window and a notification when a reminder goes
    off. Closing the window hides it until another reminder goes off."""
    WINDOW_DAYS = 8  # how far ahead events are cached for reminders

    def __init__(self, store: Store, notifier, window: ReminderWindow, enabled):
        super().__init__(window)
        self.store, self.notifier, self.window = store, notifier, window
        self._enabled = enabled  # () -> bool, read each time (Preferences can change it)
        self._notified: set = set()
        self._closed_with: set = set()
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.check)
        window.finished.connect(lambda _: self._closed_with.update(window.all_keys()))

    def start(self):
        self._timer.start()
        self.check()

    def due(self, now: dt.datetime | None = None) -> list:
        now = now or dt.datetime.now().astimezone()
        today = now.date()
        rows = self.store.events_with_reminders(
            *period(today - dt.timedelta(days=1), today + dt.timedelta(days=self.WINDOW_DAYS)))
        return (due_reminders(rows, now, self.store.reminder_states())
                + due_tasks(self.store.tasks_with_reminders(), now))

    def check(self, now: dt.datetime | None = None):
        now = now or dt.datetime.now().astimezone()
        if not self._enabled():
            self.window.hide()
            return
        self._task_summary(now)
        due = self.due(now)
        keys = {reminder_key(r) for r in due}
        if not due:
            self.window.hide()
            self._closed_with.clear()
            return
        fresh = [r for r in due if reminder_key(r) not in self._notified]
        for r in fresh:
            self._notified.add(reminder_key(r))
            if is_task(r):
                self.notifier.notify(r["title"] or "(no subject)", _task_due(r, now),
                                     f"event|task:{r['id']}", kind="reminder")
                continue
            start, _ = event_span(r)
            when = "All day" if r["all_day"] else fmt_time(start)
            self.notifier.notify(r["subject"] or "(no title)",
                                 f"{when} · {due_text(r, now)}"
                                 + (f"\n{r['location']}" if r["location"] else ""),
                                 f"event|{r['id']}", kind="reminder")
        self.window.set_reminders(due, now)
        self._closed_with &= keys  # forget closed-on reminders that are gone
        if not self.window.isVisible() and keys - self._closed_with:
            self.window.show()  # something you haven't closed the window on yet
        if fresh:
            self.window.raise_()

    def _task_summary(self, now: dt.datetime):
        """Once a day (the first check after 8:00): how many tasks are overdue and due today.
        Quieter than reminders: only when there are some, and never twice in a day."""
        today = now.date().isoformat()
        if now.hour < SUMMARY_HOUR or self.store.get_state("tasks:summary") == today:
            return
        self.store.set_state("tasks:summary", today)
        rows = [r for r in self.store.tasks(completed=False) if r["due"]]
        late = sum(r["due"] < today for r in rows)
        due = sum(r["due"] == today for r in rows)
        parts = ([f"{late} overdue"] if late else []) + ([f"{due} due today"] if due else [])
        if parts:
            self.notifier.notify("Tasks", " · ".join(parts), "tasks|", kind="reminder")
