"""The event editor: an appointment, or a meeting once it has attendees.

Details: title, attendees (required / optional, completed like To), location, start and end
or all day, Teams meeting, Show as, categories, reminder, calendar, private, attachments,
notes (formatted, with pictures, like a message).
Scheduling: everyone's free/busy for the day (Microsoft Graph getSchedule) in a grid; click a
time to move the meeting there, or "Find next free time" to jump to the first slot all
attendees are free.
"""
from __future__ import annotations

import datetime as dt
import json
from email.utils import parseaddr

from PySide6.QtCore import QDate, QRectF, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QTabWidget, QTimeEdit,
    QToolButton, QVBoxLayout, QWidget,
)

from ..calendars import (
    REMINDERS, SHOW_AS, EventDraft, busy_periods, event_span, find_free_time,
)
from ..contacts import format_address
from ..inline import extract_inline
from . import iconset
from .attachment_bar import AttachmentBar
from .recipients import RecipientEdit
from .recurrence_dialog import RepeatPicker
from .render import quotable_html
from .rich_editor import RichEditor

STATUS_COLORS = {"busy": "#4f8cff", "tentative": "#4f8cff", "oof": "#8764b8",
                 "workingElsewhere": "#30c6cc", "unknown": "#a0a0a0"}
STATUS_TEXT = {"busy": "Busy", "tentative": "Tentative", "oof": "Out of office",
               "workingElsewhere": "Working elsewhere", "unknown": "No information"}


def color_icon(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), 3, 3)
    p.end()
    return QIcon(pm)


class SchedulingGrid(QWidget):
    """Rows: people; columns: half hours of one day. Coloured where someone is busy; the
    meeting is outlined. Click a column to start the meeting there."""
    slotClicked = Signal(object)  # local datetime

    FIRST_HOUR, LAST_HOUR, NAME_W, ROW_H, HEADER_H = 7, 20, 170, 24, 22

    def __init__(self):
        super().__init__()
        self.day = dt.date.today()
        self.people: list[tuple[str, list | None]] = []  # (label, busy periods or None)
        self.meeting: tuple[dt.datetime, dt.datetime] | None = None
        self.setMinimumHeight(140)

    def set_data(self, day, people, meeting):
        self.day, self.people, self.meeting = day, people, meeting
        self.setMinimumHeight(self.HEADER_H + self.ROW_H * max(len(people), 3) + 4)
        self.update()

    def _slot_w(self) -> float:
        return (self.width() - self.NAME_W) / ((self.LAST_HOUR - self.FIRST_HOUR) * 2)

    def _x(self, t: dt.datetime) -> float:
        start = dt.datetime.combine(self.day, dt.time(self.FIRST_HOUR), t.tzinfo)
        return self.NAME_W + (t - start).total_seconds() / 1800 * self._slot_w()

    def paintEvent(self, _):
        p = QPainter(self)
        pal = self.palette()
        text, dim = pal.color(QPalette.ColorRole.Text), pal.color(QPalette.ColorRole.PlaceholderText)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        w = self._slot_w()
        tz = dt.datetime.now().astimezone().tzinfo
        p.setPen(dim)
        for i, hour in enumerate(range(self.FIRST_HOUR, self.LAST_HOUR)):
            x = self.NAME_W + i * 2 * w
            p.drawText(QRectF(x + 2, 0, 2 * w, self.HEADER_H), Qt.AlignmentFlag.AlignVCenter,
                       f"{hour}:00")
        grid = QColor(dim)
        grid.setAlpha(60)
        for row, (label, busy) in enumerate(self.people):
            y = self.HEADER_H + row * self.ROW_H
            p.setPen(text)
            p.drawText(QRectF(4, y, self.NAME_W - 8, self.ROW_H),
                       Qt.AlignmentFlag.AlignVCenter, p.fontMetrics().elidedText(
                           label, Qt.TextElideMode.ElideRight, self.NAME_W - 8))
            if busy is None:  # unknown (outside the organisation, or still loading)
                p.fillRect(QRectF(self.NAME_W, y + 3, self.width() - self.NAME_W,
                                  self.ROW_H - 6), QBrush(grid, Qt.BrushStyle.BDiagPattern))
            for start, end, status in busy or []:
                if status == "free":
                    continue
                x1, x2 = max(self._x(start), self.NAME_W), min(self._x(end), self.width())
                if x2 <= x1:
                    continue
                color = QColor(STATUS_COLORS.get(status, "#4f8cff"))
                style = (Qt.BrushStyle.BDiagPattern if status == "tentative"
                         else Qt.BrushStyle.SolidPattern)
                p.fillRect(QRectF(x1, y + 3, x2 - x1, self.ROW_H - 6), QBrush(color, style))
            p.setPen(grid)
            p.drawLine(0, y + self.ROW_H, self.width(), y + self.ROW_H)
        for i in range((self.LAST_HOUR - self.FIRST_HOUR) * 2 + 1):
            x = self.NAME_W + i * w
            p.setPen(grid)
            p.drawLine(int(x), self.HEADER_H, int(x), self.HEADER_H + len(self.people) * self.ROW_H)
        if self.meeting:
            s, e = self.meeting
            if s.date() == self.day:
                x1 = max(self._x(s.astimezone(tz)), self.NAME_W)
                x2 = min(self._x(e.astimezone(tz)), self.width())
                pen = QPen(QColor("#2f9e44"), 2)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(x1, self.HEADER_H, max(x2 - x1, 2),
                                  len(self.people) * self.ROW_H))

    def mousePressEvent(self, event):
        x = event.position().x()
        if x < self.NAME_W:
            return
        slot = int((x - self.NAME_W) // self._slot_w())
        start = dt.datetime.combine(self.day, dt.time(self.FIRST_HOUR)).astimezone() + \
            dt.timedelta(minutes=30 * slot)
        self.slotClicked.emit(start)


class EventEditor(QDialog):
    """`schedule(addresses, day, done)` fetches free/busy in the background and calls
    done(list of getSchedule entries); `me` is your own address (shown first)."""

    def __init__(self, calendars: list, categories: dict[str, str], suggestions: list,
                 schedule=None, me: str = "", row=None, start: dt.datetime | None = None,
                 end: dt.datetime | None = None, all_day: bool = False,
                 recurrence: dict | None = None, series: bool = False, tz: str = "UTC",
                 parent=None):
        """`series`: editing a whole repeating series (`row` is its first occurrence, as the
        series master has it, and `recurrence` its pattern). A `row` that is one occurrence
        of a series (opened on its own) can't change how the series repeats."""
        super().__init__(parent)
        self.row = row
        self._schedule, self._me = schedule, me
        self._body_changed = False
        self.had_pictures = False  # the loaded notes had pictures (saving may remove some)
        self.series, self.tz = series, tz
        self.occurrence = bool(row is not None and not series and row["series_id"])
        self.setWindowTitle("Edit event" if row else "New event")
        self.resize(780, 720)
        outer = QVBoxLayout(self)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Add a title")
        font = QFont(self.title.font())
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.title.setFont(font)
        outer.addWidget(self.title)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        details = QWidget()
        form = QFormLayout(details)
        self.required = RecipientEdit(suggestions)
        self.required.setPlaceholderText("Invite people (leave empty for an appointment)")
        self.optional = RecipientEdit(suggestions)
        self.optional.setPlaceholderText("Optional attendees")
        form.addRow("Attendees:", self.required)
        form.addRow("Optional:", self.optional)
        self.location = QLineEdit()
        form.addRow("Location:", self.location)

        self.start_date, self.end_date = QDateEdit(), QDateEdit()
        self.start_time, self.end_time = QTimeEdit(), QTimeEdit()
        for w in (self.start_date, self.end_date):
            w.setCalendarPopup(True)
        self.all_day = QCheckBox("All day")
        when = QHBoxLayout()
        for w in (self.start_date, self.start_time, QLabel("to"), self.end_date, self.end_time):
            when.addWidget(w)
        when.addWidget(self.all_day)
        when.addStretch()
        form.addRow("When:", when)
        self.repeat_picker = RepeatPicker(lambda: self._start().date(), tz)
        self.repeat, self.repeat_note = self.repeat_picker.combo, self.repeat_picker.note
        form.addRow("Repeat:", self.repeat_picker)
        self.teams = QCheckBox("Teams meeting (adds a join link)")
        form.addRow("", self.teams)

        self.show_as = QComboBox()
        for value, label in SHOW_AS:
            self.show_as.addItem(label, value)
        self.reminder = QComboBox()
        for minutes, label in REMINDERS:
            self.reminder.addItem(label, minutes)
        line = QHBoxLayout()
        line.addWidget(QLabel("Show as:"))
        line.addWidget(self.show_as)
        line.addSpacing(16)
        line.addWidget(QLabel("Reminder:"))
        line.addWidget(self.reminder)
        line.addStretch()
        form.addRow(line)

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
        self.calendar = QComboBox()
        for cal in calendars:
            if cal["can_edit"]:
                self.calendar.addItem(color_icon(cal["color"] or "#4f8cff"), cal["name"],
                                      cal["id"])
        shown = next((i for i in range(self.calendar.count()) if any(
            c["id"] == self.calendar.itemData(i) and c["visible"] for c in calendars)), 0)
        self.calendar.setCurrentIndex(shown)  # a new event: in a calendar you're showing
        self.private = QCheckBox("Private")
        line = QHBoxLayout()
        line.addWidget(QLabel("Categories:"))
        line.addWidget(self.category_button)
        line.addSpacing(16)
        line.addWidget(QLabel("Calendar:"))
        line.addWidget(self.calendar)
        line.addSpacing(16)
        line.addWidget(self.private)
        line.addStretch()
        form.addRow(line)
        self.attachments = AttachmentBar()
        # an existing event's attachments arrive after it opens (set_attachments); until then
        # the ones it has are left alone when saving
        self.attachments_known = row is None
        self.attachments_changed = False  # (only then is anything sent about them)
        self.attachments.changed.connect(lambda: setattr(self, "attachments_changed", True))
        form.addRow("Attachments:", self.attachments)
        self.notes = RichEditor()
        self.notes.edit.setPlaceholderText("Notes, agenda…")
        self.notes.edit.textChanged.connect(self._notes_edited)
        form.addRow(self.notes)
        self.tabs.addTab(details, "Details")

        sched = QWidget()
        slay = QVBoxLayout(sched)
        top = QHBoxLayout()
        self.sched_day = QDateEdit()
        self.sched_day.setCalendarPopup(True)
        self.find_button = QPushButton(iconset.load("search"), "Find next free time")
        self.find_button.clicked.connect(self.find_free_time)
        self.strict = QCheckBox("Avoid tentative too")
        top.addWidget(QLabel("Day:"))
        top.addWidget(self.sched_day)
        top.addStretch()
        top.addWidget(self.strict)
        top.addWidget(self.find_button)
        slay.addLayout(top)
        self.grid = SchedulingGrid()
        self.grid.slotClicked.connect(self._move_to)
        slay.addWidget(self.grid, 1)
        self.sched_status = QLabel()
        self.sched_status.setWordWrap(True)
        slay.addWidget(self.sched_status)
        legend = QLabel("<span style='color:#4f8cff'>■</span> Busy &nbsp; "
                        "<span style='color:#8764b8'>■</span> Out of office &nbsp; "
                        "<span style='color:#30c6cc'>■</span> Working elsewhere &nbsp; "
                        "hatched: tentative / no information &nbsp; "
                        "<span style='color:#2f9e44'>□</span> this meeting")
        legend.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        slay.addWidget(legend)
        self.tabs.addTab(sched, "Scheduling")
        if schedule is None:
            self.tabs.setTabEnabled(1, False)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        self._busy: dict[str, list] = {}
        self._sched_key = None
        self._sched_timer = QTimer(self)
        self._sched_timer.setSingleShot(True)
        self._sched_timer.setInterval(500)
        self._sched_timer.timeout.connect(self._load_schedule)
        for sig in (self.required.textChanged, self.optional.textChanged):
            sig.connect(lambda _: self._attendees_changed())
        self.start_date.dateChanged.connect(self._start_moved)
        self.start_time.timeChanged.connect(self._start_moved)
        self.end_date.dateChanged.connect(lambda _: self._refresh_grid())
        self.end_time.timeChanged.connect(lambda _: self._refresh_grid())
        self.all_day.toggled.connect(self._all_day_toggled)
        self.sched_day.dateChanged.connect(lambda _: self._sched_timer.start())
        self.calendar.currentIndexChanged.connect(lambda _: self._teams_allowed())
        self.tabs.currentChanged.connect(lambda i: i == 1 and self._sched_timer.start())
        self._calendars = {c["id"]: c for c in calendars}

        if row is not None:
            self._load(row)
        else:
            start = start or _next_half_hour()
            self._set_times(start, end or start + dt.timedelta(minutes=30))
            self.all_day.setChecked(all_day)
            self.show_as.setCurrentIndex(self.show_as.findData("free" if all_day else "busy"))
            self.reminder.setCurrentIndex(self.reminder.findData(15))
        self._last_start = self._start()
        self.repeat_picker.refresh(keep=False)
        if recurrence:
            self.repeat_picker.load(recurrence)
        if self.occurrence:
            self.repeat.setEnabled(False)
            self.repeat_note.setText("One occurrence of a repeating event")
        self._teams_allowed()
        self._show_categories()
        self._attendees_changed()
        self.title.setFocus()

    # -- load ------------------------------------------------------------------------
    def _load(self, row):
        self.title.setText(row["subject"] or "")
        people = json.loads(row["attendees"] or "[]")
        self.required.setText("; ".join(format_address(n, a) for n, a, kind, _ in people
                                        if kind != "optional"))
        self.optional.setText("; ".join(format_address(n, a) for n, a, kind, _ in people
                                        if kind == "optional"))
        self.location.setText(row["location"] or "")
        start, end = event_span(row)
        self.all_day.setChecked(bool(row["all_day"]))
        self._set_times(start, end - dt.timedelta(days=1) if row["all_day"] else end)
        self.teams.setChecked(bool(row["online_url"]))
        self.show_as.setCurrentIndex(max(self.show_as.findData(row["show_as"]), 0))
        self.reminder.setCurrentIndex(max(self.reminder.findData(row["reminder"]), 0))
        for name in json.loads(row["categories"] or "[]"):
            if name in self._category_actions:
                self._category_actions[name].setChecked(True)
        self.calendar.setCurrentIndex(max(self.calendar.findData(row["calendar_id"]), 0))
        self.calendar.setEnabled(False)  # moving between calendars isn't offered
        self.private.setChecked(row["sensitivity"] == "private")
        self.notes.edit.blockSignals(True)
        self.notes.edit.setPlainText(row["preview"] or "")
        self.notes.edit.blockSignals(False)

    def set_attachments(self, existing: list):
        """The event's attachments as they are on the server (loaded after opening)."""
        self.attachments.add(existing)
        self.attachments_known = True
        self.attachments_changed = False  # loading them isn't a change

    def set_body(self, content_type: str, content: str):
        """The full notes, loaded after the dialog opened (unless the user already typed);
        pictures as data: URIs (inline.resolve_cids)."""
        if self._body_changed:
            return
        edit = self.notes.edit
        edit.blockSignals(True)
        if content_type == "html":
            edit.setHtml(quotable_html(content_type, content))
            self.had_pictures = "data:image" in content
        else:
            edit.setPlainText(content)
        edit.document().clearUndoRedoStacks()
        edit.blockSignals(False)

    def _notes_edited(self):
        self._body_changed = True

    # -- times --------------------------------------------------------------------------
    def _set_times(self, start: dt.datetime, end: dt.datetime):
        for w in (self.start_date, self.start_time, self.end_date, self.end_time):
            w.blockSignals(True)
        self.start_date.setDate(QDate(start.year, start.month, start.day))
        self.start_time.setTime(QTime(start.hour, start.minute))
        self.end_date.setDate(QDate(end.year, end.month, end.day))
        self.end_time.setTime(QTime(end.hour, end.minute))
        for w in (self.start_date, self.start_time, self.end_date, self.end_time):
            w.blockSignals(False)
        self.sched_day.setDate(QDate(start.year, start.month, start.day))
        self._last_start = start
        self._refresh_grid()

    def _start(self) -> dt.datetime:
        d, t = self.start_date.date(), self.start_time.time()
        return dt.datetime(d.year(), d.month(), d.day(), t.hour(), t.minute()).astimezone()

    def _end(self) -> dt.datetime:
        d, t = self.end_date.date(), self.end_time.time()
        return dt.datetime(d.year(), d.month(), d.day(), t.hour(), t.minute()).astimezone()

    def _start_moved(self, *_):
        """Moving the start moves the end too (same length), as Outlook does."""
        duration = self._end() - self._last_start
        start = self._start()
        self._set_times(start, start + max(duration, dt.timedelta(0)))
        self.repeat_picker.refresh()  # "Weekly on Monday" follows the date

    # -- repeat ---------------------------------------------------------------------------
    def recurrence(self) -> dict | None:
        """The chosen repeat pattern (a Graph recurrence), or None."""
        return self.repeat_picker.recurrence()

    def _all_day_toggled(self, on: bool):
        self.start_time.setEnabled(not on)
        self.end_time.setEnabled(not on)
        if on and self.show_as.currentData() == "busy" and self.row is None:
            self.show_as.setCurrentIndex(self.show_as.findData("free"))
        self._refresh_grid()

    def _move_to(self, start: dt.datetime):
        duration = self._end() - self._start()
        self._set_times(start, start + duration)

    # -- categories, Teams ------------------------------------------------------------
    def categories(self) -> list[str]:
        return [n for n, a in self._category_actions.items() if a.isChecked()]

    def _show_categories(self):
        chosen = self.categories()
        self.category_button.setText(", ".join(chosen) if chosen else "None")
        icon = (self._category_actions[chosen[0]].icon() if chosen else QIcon())
        self.category_button.setIcon(icon)

    def _teams_allowed(self):
        cal = self._calendars.get(self.calendar.currentData())
        allowed = bool(cal and cal["teams"])
        self.teams.setEnabled(allowed or self.teams.isChecked())
        self.teams.setToolTip("" if allowed else
                              "This calendar's account doesn't offer Teams meetings")

    # -- attendees and free/busy -------------------------------------------------------------
    def attendees(self) -> tuple[list, list]:
        return ([parseaddr(a) for a in self.required.addresses()],
                [parseaddr(a) for a in self.optional.addresses()])

    def _attendees_changed(self):
        req, opt = self.attendees()
        meeting = bool(req or opt)
        self.save_button.setText("Send" if meeting else "Save")
        what = "meeting" if meeting else "event"
        if self.series:
            what += " series"
        elif self.occurrence:
            what = "occurrence"
        self.setWindowTitle(("Edit " if self.row else "New ") + what)
        self._sched_timer.start()

    def _load_schedule(self):
        if self._schedule is None or self.tabs.currentIndex() != 1:
            self._refresh_grid()
            return
        req, opt = self.attendees()
        addresses = [a for a in [self._me] + [a for _, a in req + opt] if a]
        day = self.sched_day.date().toPython()
        key = (tuple(addresses), day)
        if key == self._sched_key:
            return
        self._sched_key = key
        self.sched_status.setText("Getting availability…")
        self._schedule(addresses, day, lambda result, k=key: self.set_schedule(k, result))

    def set_schedule(self, key, result: list[dict]):
        if key != self._sched_key:  # an older answer
            return
        self._busy = {}
        for entry in result:
            if entry.get("error"):
                continue
            self._busy[(entry.get("scheduleId") or "").lower()] = busy_periods(entry)
        self._refresh_grid()

    def _refresh_grid(self):
        req, opt = self.attendees()
        people = [("You", self._busy.get(self._me.lower()))] if self._me else []
        people += [(n or a, self._busy.get(a.lower())) for n, a in req]
        people += [(f"{n or a} (optional)", self._busy.get(a.lower())) for n, a in opt]
        start, end = self._start(), self._end()
        self.grid.set_data(self.sched_day.date().toPython(), people,
                           None if self.all_day.isChecked() else (start, end))
        clashes = [label for label, busy in people
                   if busy and any(s < end and e > start and st in ("busy", "oof")
                                   for s, e, st in busy)]
        if not self._busy and self._schedule is not None and len(people) > 1:
            self.sched_status.setText("Open this tab to see everyone's availability.")
        elif clashes:
            self.sched_status.setText("Not free then: " + ", ".join(clashes))
        elif self._busy:
            self.sched_status.setText("Everyone is free at this time.")
        else:
            self.sched_status.setText("")

    def find_free_time(self):
        busy = [b for _, b in [("You", self._busy.get(self._me.lower()))] +
                [(a, self._busy.get(a.lower())) for _, a in sum(self.attendees(), [])] if b]
        duration = max(self._end() - self._start(), dt.timedelta(minutes=30))
        found = find_free_time(busy, self._start() + dt.timedelta(minutes=30), duration,
                               strict=self.strict.isChecked())
        if found is None:
            self.sched_status.setText("No time when everyone is free in the next two weeks.")
            return
        self._set_times(found, found + duration)
        self._sched_timer.start()

    # -- result ----------------------------------------------------------------------------
    def _accept(self):
        if not self.title.text().strip():
            self.title.setFocus()
            self.title.setPlaceholderText("A title, please")
            return
        if not self.all_day.isChecked() and self._end() <= self._start():
            self.end_time.setFocus()
            return
        self.accept()

    def calendar_id(self) -> str:
        return self.calendar.currentData() or ""

    def draft(self) -> EventDraft:
        req, opt = self.attendees()
        start, end = self._start(), self._end()
        if self.all_day.isChecked():  # the end date is the last day; Graph wants the day after
            end = dt.datetime.combine(self.end_date.date().toPython() + dt.timedelta(days=1),
                                      dt.time()).astimezone()
        return EventDraft(
            subject=self.title.text().strip(), start=start, end=end,
            all_day=self.all_day.isChecked(), location=self.location.text().strip(),
            required=tuple(req), optional=tuple(opt), teams=self.teams.isChecked(),
            show_as=self.show_as.currentData(), categories=tuple(self.categories()),
            reminder=self.reminder.currentData(),
            **self._notes(),
            private=self.private.isChecked(),
            recurrence=None if self.occurrence else self.recurrence(),
            stop_repeating=self.series and self.recurrence() is None, tz=self.tz)


    def _notes(self) -> dict:
        """body, body_html, inline for the draft: HTML (pictures as cid: links), or plain
        text when there's no formatting to keep. Left out if they weren't touched."""
        if not (self._body_changed or self.row is None):
            return {}
        html = self.notes.html()
        if not self.notes.text().strip() and "<img" not in html:
            return {"body": ""}
        body, inline = extract_inline(html)
        return {"body": body, "body_html": True, "inline": tuple(inline)}


def _next_half_hour() -> dt.datetime:
    now = dt.datetime.now().astimezone().replace(second=0, microsecond=0)
    return now + dt.timedelta(minutes=30 - now.minute % 30)
