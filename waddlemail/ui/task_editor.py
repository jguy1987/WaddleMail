"""The task editor, as Outlook 2010's task form: subject, list, start and due dates (with the
quick follow-up dates), status, priority, reminder, categories, notes, and the email a task
was made from."""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import QDate, Qt, QTime, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QTimeEdit, QToolButton,
    QVBoxLayout,
)

from ..tasks import IMPORTANCE, QUICK, STATUS, TaskDraft, quick_due
from . import iconset
from .event_editor import color_icon

REMINDER_TIME = QTime(8, 0)  # Outlook's: 8:00 on the due date


class TaskEditor(QDialog):
    openEmailRequested = Signal(str)  # task id

    def __init__(self, lists: list, categories: dict[str, str], row=None,
                 list_id: str | None = None, title: str = "", body: str = "",
                 due: dt.date | None = None, links: list | None = None, parent=None):
        """`lists`: task_lists rows to choose from; `row`: the task to edit (else a new one,
        in `list_id`, filled with `title`, `body`, `due` and `links`: [(name, url, id)])."""
        super().__init__(parent)
        self.row = row
        self.links = links or []
        self.setWindowTitle("Task" if row else "New task")
        self.setWindowIcon(iconset.load("square-check-big"))
        self.resize(560, 520)
        outer = QVBoxLayout(self)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Subject")
        font = QFont(self.title.font())
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.title.setFont(font)
        outer.addWidget(self.title)
        form = self._form = QFormLayout()
        outer.addLayout(form)

        self.list = QComboBox()
        accounts = {r["account_id"] for r in lists}
        for r in lists:
            label = r["name"] if len(accounts) == 1 else f"{r['name']} ({r['account_id']})"
            self.list.addItem(label, r["id"])
        form.addRow("List:", self.list)

        today = QDate.currentDate()
        self.has_start, self.start = QCheckBox(), QDateEdit(today)
        self.has_due, self.due = QCheckBox(), QDateEdit(today)
        for check, edit in ((self.has_start, self.start), (self.has_due, self.due)):
            edit.setCalendarPopup(True)
            edit.setEnabled(False)
            check.toggled.connect(edit.setEnabled)
        self.quick = QToolButton()
        self.quick.setText("Follow up")
        self.quick.setIcon(iconset.load("flag"))
        self.quick.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.quick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        for key, label in QUICK:
            menu.addAction(label, lambda k=key: self.set_due(quick_due(k, dt.date.today())))
        self.quick.setMenu(menu)
        line = QHBoxLayout()
        line.addWidget(self.has_start)
        line.addWidget(self.start)
        line.addSpacing(16)
        line.addWidget(QLabel("Due:"))
        line.addWidget(self.has_due)
        line.addWidget(self.due)
        line.addWidget(self.quick)
        line.addStretch()
        form.addRow("Start:", line)

        self.status, self.importance = QComboBox(), QComboBox()
        for key, label in STATUS:
            self.status.addItem(label, key)
        for key, label in IMPORTANCE:
            self.importance.addItem(label, key)
        self.importance.setCurrentIndex(1)
        line = QHBoxLayout()
        line.addWidget(self.status)
        line.addSpacing(16)
        line.addWidget(QLabel("Priority:"))
        line.addWidget(self.importance)
        line.addStretch()
        form.addRow("Status:", line)

        self.has_reminder = QCheckBox()
        self.remind_day, self.remind_time = QDateEdit(today), QTimeEdit(REMINDER_TIME)
        self.remind_day.setCalendarPopup(True)
        for w in (self.remind_day, self.remind_time):
            w.setEnabled(False)
            self.has_reminder.toggled.connect(w.setEnabled)
        self.has_reminder.toggled.connect(self._reminder_ticked)
        line = QHBoxLayout()
        line.addWidget(self.has_reminder)
        line.addWidget(self.remind_day)
        line.addWidget(self.remind_time)
        line.addStretch()
        form.addRow("Reminder:", line)

        self.category_button = QToolButton()
        self.category_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.category_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        menu = QMenu(self)
        self._category_actions = {}
        for name, color in categories.items():
            act = menu.addAction(color_icon(color), name)
            act.setCheckable(True)
            act.toggled.connect(lambda _=False: self._show_categories())
            self._category_actions[name] = act
        if not categories:
            menu.addAction("(no categories in your Outlook)").setEnabled(False)
        self.category_button.setMenu(menu)
        form.addRow("Categories:", self.category_button)

        self.email = QLabel()
        self.email.setWordWrap(True)
        self.open_email = QPushButton(iconset.load("mail"), "Open email")
        self.open_email.clicked.connect(
            lambda: self.row is not None and self.openEmailRequested.emit(self.row["id"]))
        line = QHBoxLayout()
        line.addWidget(self.email, 1)
        line.addWidget(self.open_email)
        self.email_row = line
        form.addRow("From email:", line)

        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Notes")
        outer.addWidget(self.notes, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        if row is not None:
            self._load(row)
        else:
            self.title.setText(title)
            self.notes.setPlainText(body)
            if list_id:
                self.list.setCurrentIndex(max(self.list.findData(list_id), 0))
            if due is not None:
                self.set_due(due)
        self.show_links([(n, u, e) for n, u, e, *_ in self.links])
        self._show_categories()
        self.title.setFocus()

    # -- filling in --------------------------------------------------------------------------
    def _load(self, row):
        self.title.setText(row["title"] or "")
        self.list.setCurrentIndex(max(self.list.findData(row["list_id"]), 0))
        for check, edit, value in ((self.has_start, self.start, row["start"]),
                                   (self.has_due, self.due, row["due"])):
            check.setChecked(bool(value))
            if value:
                edit.setDate(QDate.fromString(value, "yyyy-MM-dd"))
        self.status.setCurrentIndex(max(self.status.findData(row["status"]), 0))
        self.importance.setCurrentIndex(max(self.importance.findData(row["importance"]), 0))
        if row["reminder"]:
            when = dt.datetime.fromisoformat(row["reminder"].replace("Z", "+00:00")).astimezone()
            self.has_reminder.blockSignals(True)  # (not the 8:00 default)
            self.has_reminder.setChecked(True)
            self.has_reminder.blockSignals(False)
            self.remind_day.setEnabled(True)
            self.remind_time.setEnabled(True)
            self.remind_day.setDate(QDate(when.year, when.month, when.day))
            self.remind_time.setTime(QTime(when.hour, when.minute))
        for name in json.loads(row["categories"] or "[]"):
            if name in self._category_actions:
                self._category_actions[name].setChecked(True)
        self.notes.setPlainText(row["body"] or "")
        self.links = json.loads(row["links"]) if row["links"] else []

    def show_links(self, links: list):
        """The email the task came from (links: [(name, url, external id)])."""
        self.links = links
        name = next((n for n, *_ in links if n), "")
        self.email.setText(name or ("(an email)" if links else ""))
        for i in range(self.email_row.count()):
            self.email_row.itemAt(i).widget().setVisible(bool(links))
        label = self._form.labelForField(self.email_row)
        if label is not None:
            label.setVisible(bool(links))
        self.open_email.setEnabled(bool(links) and self.row is not None)

    def set_due(self, day: dt.date | None):
        self.has_due.setChecked(day is not None)
        if day is not None:
            self.due.setDate(QDate(day.year, day.month, day.day))

    def _reminder_ticked(self, on: bool):
        if on:  # Outlook's default: 8:00 on the due date (or today)
            self.remind_day.setDate(self.due.date() if self.has_due.isChecked()
                                    else QDate.currentDate())
            self.remind_time.setTime(REMINDER_TIME)

    def categories(self) -> list[str]:
        return [n for n, a in self._category_actions.items() if a.isChecked()]

    def _show_categories(self):
        chosen = self.categories()
        self.category_button.setText(", ".join(chosen) if chosen else "None")
        self.category_button.setIcon(self._category_actions[chosen[0]].icon() if chosen
                                     else QIcon())

    # -- result ------------------------------------------------------------------------------
    def _accept(self):
        if not self.title.text().strip():
            self.title.setFocus()
            self.title.setPlaceholderText("A subject, please")
            return
        self.accept()

    def list_id(self) -> str:
        return self.list.currentData() or ""

    def draft(self, tz: str) -> TaskDraft:
        reminder = None
        if self.has_reminder.isChecked():
            d, t = self.remind_day.date().toPython(), self.remind_time.time().toPython()
            reminder = dt.datetime.combine(d, t).astimezone()
        return TaskDraft(
            title=self.title.text().strip(),
            due=self.due.date().toPython() if self.has_due.isChecked() else None,
            start=self.start.date().toPython() if self.has_start.isChecked() else None,
            reminder=reminder, importance=self.importance.currentData(),
            status=self.status.currentData(), categories=tuple(self.categories()),
            body=self.notes.toPlainText(),
            links=[] if self.row is not None else [(n, u, e) for n, u, e in self.links],
            tz=tz)
