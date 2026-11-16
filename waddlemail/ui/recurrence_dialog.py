"""Repeating: RepeatPicker, the "Repeat" choice of the event and task editors (Does not repeat,
Outlook's presets for the day, Custom…), and RecurrenceDialog, the custom pattern: every N
days / weeks (on chosen weekdays) / months (on a day, or on the Nth weekday) / years, ending
never, on a date, or after a number of times."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from PySide6.QtCore import QDate, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QRadioButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..calendars import (
    MONTHS, ORDINALS, WEEKDAYS, describe_recurrence, make_recurrence, ordinal_of, presets,
)

KINDS = [("daily", "Daily", "day"), ("weekly", "Weekly", "week"),
         ("monthly", "Monthly", "month"), ("yearly", "Yearly", "year")]


def _row(*widgets) -> QHBoxLayout:
    lay = QHBoxLayout()
    for w in widgets:
        lay.addWidget(QLabel(w) if isinstance(w, str) else w)
    lay.addStretch()
    return lay


def _weekday_combo(selected: str) -> QComboBox:
    box = QComboBox()
    for d in WEEKDAYS:
        box.addItem(d.capitalize(), d)
    box.setCurrentIndex(WEEKDAYS.index(selected))
    return box


def _ordinal_combo(selected: str) -> QComboBox:
    box = QComboBox()
    for o in ORDINALS:
        box.addItem(o, o)
    box.setCurrentIndex(ORDINALS.index(selected))
    return box


def _month_combo(month: int) -> QComboBox:
    box = QComboBox()
    for i, m in enumerate(MONTHS):
        box.addItem(m, i + 1)
    box.setCurrentIndex(month - 1)
    return box


class RecurrenceDialog(QDialog):
    def __init__(self, start: dt.date, recurrence: dict | None = None, tz: str = "UTC",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Repeat")
        self.start, self.tz = start, tz
        wd, nth = WEEKDAYS[start.weekday()], ordinal_of(start)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for key, label, _ in KINDS:
            self.kind.addItem(label, key)
        self.interval = QSpinBox()
        self.interval.setRange(1, 999)
        self.unit = QLabel()
        form.addRow("Repeat:", _row(self.kind, "every", self.interval, self.unit))

        self.pages = QStackedWidget()
        self.pages.addWidget(QWidget())  # daily: nothing more
        weekly = QWidget()
        self.days = {}
        wlay = QHBoxLayout(weekly)
        wlay.setContentsMargins(0, 0, 0, 0)
        for d in WEEKDAYS:
            box = QCheckBox(d[:3].capitalize())
            box.setChecked(d == wd)
            self.days[d] = box
            wlay.addWidget(box)
        wlay.addStretch()
        self.pages.addWidget(weekly)

        monthly = QWidget()
        mlay = QVBoxLayout(monthly)
        mlay.setContentsMargins(0, 0, 0, 0)
        self.m_on_day, self.m_on_nth = QRadioButton("On day"), QRadioButton("On the")
        self.m_day = QSpinBox()
        self.m_day.setRange(1, 31)
        self.m_day.setValue(start.day)
        self.m_ordinal, self.m_weekday = _ordinal_combo(nth), _weekday_combo(wd)
        mlay.addLayout(_row(self.m_on_day, self.m_day))
        mlay.addLayout(_row(self.m_on_nth, self.m_ordinal, self.m_weekday))
        self.m_on_day.setChecked(True)
        group = QButtonGroup(monthly)
        group.addButton(self.m_on_day)
        group.addButton(self.m_on_nth)
        self.pages.addWidget(monthly)

        yearly = QWidget()
        ylay = QVBoxLayout(yearly)
        ylay.setContentsMargins(0, 0, 0, 0)
        self.y_on_date, self.y_on_nth = QRadioButton("On"), QRadioButton("On the")
        self.y_month, self.y_day = _month_combo(start.month), QSpinBox()
        self.y_day.setRange(1, 31)
        self.y_day.setValue(start.day)
        self.y_ordinal, self.y_weekday = _ordinal_combo(nth), _weekday_combo(wd)
        self.y_month2 = _month_combo(start.month)
        ylay.addLayout(_row(self.y_on_date, self.y_month, self.y_day))
        ylay.addLayout(_row(self.y_on_nth, self.y_ordinal, self.y_weekday, "of", self.y_month2))
        self.y_on_date.setChecked(True)
        group = QButtonGroup(yearly)
        group.addButton(self.y_on_date)
        group.addButton(self.y_on_nth)
        self.pages.addWidget(yearly)
        form.addRow("", self.pages)

        self.end_never, self.end_on, self.end_after = (QRadioButton("Never"),
                                                       QRadioButton("On"),
                                                       QRadioButton("After"))
        self.end_date = QDateEdit(QDate(start.year, start.month, start.day).addMonths(3))
        self.end_date.setCalendarPopup(True)
        self.count = QSpinBox()
        self.count.setRange(1, 999)
        self.count.setValue(10)
        self.end_never.setChecked(True)
        ends = QVBoxLayout()
        ends.addLayout(_row(self.end_never))
        ends.addLayout(_row(self.end_on, self.end_date))
        ends.addLayout(_row(self.end_after, self.count, "times"))
        form.addRow("Ends:", ends)
        lay.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        if recurrence:
            self._load(recurrence)
        else:
            self.kind.setCurrentIndex(1)  # weekly, the usual
        self.kind.currentIndexChanged.connect(self._kind_changed)
        for kind, signal in ((QComboBox, "currentIndexChanged"), (QSpinBox, "valueChanged"),
                             (QCheckBox, "toggled"), (QRadioButton, "toggled"),
                             (QDateEdit, "dateChanged")):
            for w in self.findChildren(kind):
                getattr(w, signal).connect(lambda *_: self._update())
        self._kind_changed()

    def _kind_changed(self, *_):
        i = self.kind.currentIndex()
        self.pages.setCurrentIndex(i)
        self._update()

    def _update(self):
        unit = KINDS[self.kind.currentIndex()][2]
        self.unit.setText(unit + ("s" if self.interval.value() > 1 else ""))
        self.summary.setText(describe_recurrence(self.recurrence()))

    def _load(self, rec: dict):
        p, r = rec.get("pattern") or {}, rec.get("range") or {}
        kind = p.get("type", "weekly")
        family = {"absoluteMonthly": "monthly", "relativeMonthly": "monthly",
                  "absoluteYearly": "yearly", "relativeYearly": "yearly"}.get(kind, kind)
        self.kind.setCurrentIndex([k for k, _, _ in KINDS].index(family))
        self.interval.setValue(p.get("interval") or 1)
        days = [d.lower() for d in p.get("daysOfWeek") or []]
        if kind == "weekly":
            for d, box in self.days.items():
                box.setChecked(d in days)
        elif kind == "absoluteMonthly":
            self.m_on_day.setChecked(True)
            self.m_day.setValue(p.get("dayOfMonth") or 1)
        elif kind == "relativeMonthly":
            self.m_on_nth.setChecked(True)
            self.m_ordinal.setCurrentIndex(ORDINALS.index(p.get("index", "first")))
            if days:
                self.m_weekday.setCurrentIndex(WEEKDAYS.index(days[0]))
        elif kind == "absoluteYearly":
            self.y_on_date.setChecked(True)
            self.y_month.setCurrentIndex((p.get("month") or 1) - 1)
            self.y_day.setValue(p.get("dayOfMonth") or 1)
        elif kind == "relativeYearly":
            self.y_on_nth.setChecked(True)
            self.y_ordinal.setCurrentIndex(ORDINALS.index(p.get("index", "first")))
            if days:
                self.y_weekday.setCurrentIndex(WEEKDAYS.index(days[0]))
            self.y_month2.setCurrentIndex((p.get("month") or 1) - 1)
        if r.get("type") == "endDate" and r.get("endDate"):
            self.end_on.setChecked(True)
            d = dt.date.fromisoformat(r["endDate"][:10])
            self.end_date.setDate(QDate(d.year, d.month, d.day))
        elif r.get("type") == "numbered":
            self.end_after.setChecked(True)
            self.count.setValue(r.get("numberOfOccurrences") or 1)

    def pattern(self) -> dict:
        n = self.interval.value()
        kind = KINDS[self.kind.currentIndex()][0]
        if kind == "daily":
            return {"type": "daily", "interval": n}
        if kind == "weekly":
            days = [d for d, box in self.days.items() if box.isChecked()] or \
                [WEEKDAYS[self.start.weekday()]]
            return {"type": "weekly", "interval": n, "daysOfWeek": days}
        if kind == "monthly":
            if self.m_on_day.isChecked():
                return {"type": "absoluteMonthly", "interval": n,
                        "dayOfMonth": self.m_day.value()}
            return {"type": "relativeMonthly", "interval": n,
                    "daysOfWeek": [self.m_weekday.currentData()],
                    "index": self.m_ordinal.currentData()}
        if self.y_on_date.isChecked():
            return {"type": "absoluteYearly", "interval": n, "month": self.y_month.currentData(),
                    "dayOfMonth": self.y_day.value()}
        return {"type": "relativeYearly", "interval": n, "month": self.y_month2.currentData(),
                "daysOfWeek": [self.y_weekday.currentData()],
                "index": self.y_ordinal.currentData()}

    def recurrence(self) -> dict:
        end = self.end_date.date().toPython() if self.end_on.isChecked() else None
        count = self.count.value() if self.end_after.isChecked() else None
        return make_recurrence(self.pattern(), self.start, end, count, self.tz)


def same_pattern(a: dict, b: dict) -> bool:
    """Equal repeat patterns, ignoring fields that don't apply (Graph fills them with 0s)."""
    def norm(p: dict) -> tuple:
        relative = p.get("type", "").startswith("relative")
        return (p.get("type"), p.get("interval") or 1,
                sorted(d.lower() for d in p.get("daysOfWeek") or []),
                None if relative else (p.get("dayOfMonth") or None),
                p.get("month") or None, (p.get("index") or "first") if relative else None)
    return norm(a) == norm(b)


class RepeatPicker(QWidget):
    """"Does not repeat", the presets for the day it starts ("Weekly on Monday", ...), and
    Custom… (RecurrenceDialog), with a note beside it. `start()` gives the day the pattern
    starts on: call refresh() when it changes, so the presets follow it."""
    changed = Signal()

    def __init__(self, start: Callable[[], dt.date], tz: str = "UTC", parent=None):
        super().__init__(parent)
        self._start, self.tz = start, tz
        self._custom: dict | None = None  # a custom pattern
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setMinimumContentsLength(28)
        self.combo.activated.connect(self._chosen)
        self.note = QLabel()
        self.note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(self.combo)
        lay.addWidget(self.note, 1)
        self._index = 0
        self.refresh(keep=False)

    def refresh(self, keep: bool = True):
        index = self.combo.currentIndex() if keep else 0
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("Does not repeat")
        for label, _ in presets(self._start()):
            self.combo.addItem(label)
        self.combo.addItem(describe_recurrence(self._custom) if self._custom else "Custom…")
        self.combo.setCurrentIndex(max(index, 0))
        self.combo.blockSignals(False)
        self._index = self.combo.currentIndex()

    def _chosen(self, index: int):
        if index == self.combo.count() - 1:  # Custom…: open the full pattern editor
            dlg = RecurrenceDialog(self._start(), self._custom or self.recurrence(), self.tz,
                                   self)
            if dlg.exec():
                self._custom = dlg.recurrence()
                self.refresh(keep=False)
                self.combo.setCurrentIndex(self.combo.count() - 1)
            else:
                self.combo.setCurrentIndex(self._index)
        self._index = self.combo.currentIndex()
        self.changed.emit()

    def load(self, rec: dict | None):
        """Show a pattern: as a preset if it is one (repeating forever), else as custom."""
        if not rec:
            self.refresh(keep=False)
            return
        pattern = rec.get("pattern") or {}
        endless = (rec.get("range") or {}).get("type") == "noEnd"
        for i, (_, preset) in enumerate(presets(self._start())):
            if endless and same_pattern(preset, pattern):
                self.refresh(keep=False)
                self.combo.setCurrentIndex(i + 1)
                self._index = i + 1
                return
        self._custom = rec
        self.refresh(keep=False)
        self.combo.setCurrentIndex(self.combo.count() - 1)
        self._index = self.combo.count() - 1

    def recurrence(self) -> dict | None:
        """The chosen pattern (a Graph recurrence starting on start()), or None."""
        index = self.combo.currentIndex()
        if index <= 0:
            return None
        if index == self.combo.count() - 1:
            return self._custom
        return make_recurrence(presets(self._start())[index - 1][1], self._start(), tz=self.tz)
