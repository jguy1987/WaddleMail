"""The Calendar page: Day / Work week / Week / Month views, a mini month and the calendar list
on the left, and the selected event's details on the right.

Events are coloured by their first category (Outlook's colours), else by their calendar. The
bar on an event's left edge shows how it shows its time, as Outlook draws it: solid for Busy,
hatched for Tentative, purple for Out of office, dotted for Working elsewhere, outline only
for Free.

In the day/week grid: drag across empty time to pick a period, double-click (or Enter) to
create an event there; click an event to see it, double-click to edit, Delete to remove.
The page reads the store; MainWindow fetches the events of each period shown
(rangeChanged) and does every change in the background.
"""
from __future__ import annotations

import datetime as dt
import html
import json

from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QCalendarWidget, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTextBrowser, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from ..calendars import (
    DEFAULT_CALENDAR_COLOR, SHOW_AS_LABEL, event_span, local_midnight, overlap_columns,
    period,
)
from ..store import Store
from . import iconset
from .event_editor import color_icon

DAY, WORK_WEEK, WEEK, MONTH = "day", "workweek", "week", "month"
SHOW_AS_COLORS = {"oof": "#8764b8", "workingElsewhere": "#30c6cc"}
RESPONSES = {"accepted": "Accepted", "tentativelyAccepted": "Tentative",
             "declined": "Declined", "none": "No response", "notResponded": "No response",
             "organizer": "Organizer"}


def first_weekday() -> int:
    """1 = Monday ... 7 = Sunday, from the locale."""
    return QLocale.system().firstDayOfWeek().value


def qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=(d.isoweekday() - first_weekday()) % 7)


class Ev:
    """An event as the views draw it."""
    __slots__ = ("row", "id", "start", "end", "color", "show_as", "all_day", "subject",
                 "location", "cancelled", "declined")

    def __init__(self, row, colors: dict[str, str]):
        self.row, self.id = row, row["id"]
        self.start, self.end = event_span(row)
        cats = json.loads(row["categories"] or "[]")
        self.color = QColor(next((colors[c] for c in cats if c in colors), None)
                            or row["calendar_color"] or DEFAULT_CALENDAR_COLOR)
        self.show_as = row["show_as"] or "busy"
        self.all_day = bool(row["all_day"])
        self.subject = row["subject"] or "(no title)"
        self.location = row["location"] or ""
        self.cancelled = bool(row["cancelled"])
        self.declined = row["response"] == "declined"


def _blend(color: QColor, base: QColor, amount: float) -> QColor:
    return QColor(int(color.red() * amount + base.red() * (1 - amount)),
                  int(color.green() * amount + base.green() * (1 - amount)),
                  int(color.blue() * amount + base.blue() * (1 - amount)))


def draw_event(p: QPainter, rect: QRectF, ev: Ev, pal: QPalette, selected: bool,
               lines: list[str]) -> None:
    """A rounded block: tinted fill, the show-as bar on the left, the text."""
    base = pal.color(QPalette.ColorRole.Base)
    text = pal.color(QPalette.ColorRole.Text)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = _blend(ev.color, base, 0.12 if ev.declined else 0.28)
    p.setPen(QPen(ev.color.darker(130) if selected else ev.color, 2 if selected else 1))
    p.setBrush(fill)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
    bar = QRectF(rect.left() + 1, rect.top() + 1, 5, rect.height() - 2)
    bar_color = QColor(SHOW_AS_COLORS.get(ev.show_as, ev.color))
    style = {"tentative": Qt.BrushStyle.BDiagPattern, "free": Qt.BrushStyle.NoBrush,
             "workingElsewhere": Qt.BrushStyle.Dense4Pattern}.get(ev.show_as,
                                                                   Qt.BrushStyle.SolidPattern)
    p.setPen(Qt.PenStyle.NoPen)
    p.fillRect(bar, base)
    p.fillRect(bar, QBrush(bar_color, style))
    if ev.show_as == "free":
        p.setPen(QPen(bar_color, 1))
        p.drawRect(bar.adjusted(0.5, 0.5, -0.5, -0.5))
    p.setClipRect(rect.adjusted(8, 1, -2, -1))
    font = QFont(p.font())
    font.setStrikeOut(ev.cancelled)
    y = rect.top() + 1
    for i, line in enumerate(lines):
        f = QFont(font)
        f.setBold(i == 0)
        p.setFont(f)
        fm = QFontMetrics(f)
        if y + fm.height() > rect.bottom() + 2 and i > 0:
            break
        c = QColor(text)
        if ev.declined:
            c.setAlpha(140)
        p.setPen(c)
        p.drawText(QRectF(rect.left() + 9, y, rect.width() - 11, fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(line, Qt.TextElideMode.ElideRight, int(rect.width() - 11)))
        y += fm.height()
    p.restore()


def fmt_time(t: dt.datetime) -> str:
    return QLocale.system().toString(t.time(), QLocale.FormatType.ShortFormat)


class TimeGrid(QWidget):
    """Hours down, days across."""
    eventClicked = Signal(str)
    eventActivated = Signal(str)
    slotActivated = Signal(object, object)  # local start, end

    HOUR_H, GUTTER = 48, 56

    def __init__(self):
        super().__init__()
        self.days: list[dt.date] = []
        self.events: list[Ev] = []
        self.selected: str | None = None
        self.selection: tuple[dt.datetime, dt.datetime] | None = None
        self._drag_from: dt.datetime | None = None
        self._boxes: list[tuple[QRectF, Ev]] = []
        self.setMinimumHeight(24 * self.HOUR_H)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)
        timer = QTimer(self)  # keep the "now" line moving
        timer.timeout.connect(self.update)
        timer.start(60_000)

    def sizeHint(self):
        return QSize(600, 24 * self.HOUR_H)

    def set_content(self, days, events):
        self.days, self.events = days, [e for e in events if not e.all_day]
        self.update()

    def _col_w(self) -> float:
        return (self.width() - self.GUTTER) / max(len(self.days), 1)

    def _layout(self):
        boxes = []
        w = self._col_w()
        for i, day in enumerate(self.days):
            start, end = local_midnight(day), local_midnight(day + dt.timedelta(days=1))
            segs = [(max(e.start, start), min(e.end, end), e) for e in self.events
                    if e.start < end and e.end > start]
            cols = overlap_columns([(s, en) for s, en, _ in segs])
            for (s, en, ev), (col, ncols) in zip(segs, cols):
                y1 = (s - start).total_seconds() / 3600 * self.HOUR_H
                y2 = (en - start).total_seconds() / 3600 * self.HOUR_H
                x = self.GUTTER + i * w + col * (w / ncols)
                boxes.append((QRectF(x + 1, y1 + 1, w / ncols - 3, max(y2 - y1 - 2, 18)), ev))
        self._boxes = boxes

    def _time_at(self, pos: QPointF) -> tuple[dt.date, dt.datetime] | None:
        if pos.x() < self.GUTTER or not self.days:
            return None
        i = min(int((pos.x() - self.GUTTER) // self._col_w()), len(self.days) - 1)
        minutes = int(pos.y() / self.HOUR_H * 60) // 30 * 30  # half-hour slots
        minutes = max(0, min(minutes, 23 * 60 + 30))
        return self.days[i], local_midnight(self.days[i]) + dt.timedelta(minutes=minutes)

    def paintEvent(self, _):
        self._layout()
        p = QPainter(self)
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Base)
        off_hours = pal.color(QPalette.ColorRole.AlternateBase)
        dim = pal.color(QPalette.ColorRole.PlaceholderText)
        accent = pal.color(QPalette.ColorRole.Highlight)
        p.fillRect(self.rect(), base)
        w = self._col_w()
        for i, day in enumerate(self.days):  # outside working hours a little darker
            x = self.GUTTER + i * w
            if day.isoweekday() >= 6:
                p.fillRect(QRectF(x, 0, w, self.height()), off_hours)
            else:
                p.fillRect(QRectF(x, 0, w, 8 * self.HOUR_H), off_hours)
                p.fillRect(QRectF(x, 18 * self.HOUR_H, w, 6 * self.HOUR_H), off_hours)
        line = QColor(dim)
        line.setAlpha(70)
        half = QColor(dim)
        half.setAlpha(30)
        for hour in range(24):
            y = hour * self.HOUR_H
            p.setPen(line)
            p.drawLine(self.GUTTER, y, self.width(), y)
            p.setPen(half)
            p.drawLine(self.GUTTER, y + self.HOUR_H // 2, self.width(), y + self.HOUR_H // 2)
            p.setPen(dim)
            label = QLocale.system().toString(dt.time(hour), QLocale.FormatType.ShortFormat)
            p.drawText(QRectF(0, y + 2, self.GUTTER - 6, 16), Qt.AlignmentFlag.AlignRight,
                       label)
        for i in range(len(self.days) + 1):
            p.setPen(line)
            x = int(self.GUTTER + i * w)
            p.drawLine(x, 0, x, self.height())
        if self.selection:
            s, e = self.selection
            for i, day in enumerate(self.days):
                ds = local_midnight(day)
                de = ds + dt.timedelta(days=1)
                a, b = max(s, ds), min(e, de)
                if b > a:
                    fill = QColor(accent)
                    fill.setAlpha(60)
                    y1 = (a - ds).total_seconds() / 3600 * self.HOUR_H
                    y2 = (b - ds).total_seconds() / 3600 * self.HOUR_H
                    p.fillRect(QRectF(self.GUTTER + i * w + 1, y1, w - 2, y2 - y1), fill)
        for rect, ev in self._boxes:
            lines = [ev.subject]
            if rect.height() >= 34:
                lines.append(f"{fmt_time(ev.start)}–{fmt_time(ev.end)}"
                             + (f"  {ev.location}" if ev.location else ""))
            draw_event(p, rect, ev, pal, ev.id == self.selected, lines)
        now = dt.datetime.now().astimezone()
        if now.date() in self.days:
            i = self.days.index(now.date())
            y = (now - local_midnight(now.date())).total_seconds() / 3600 * self.HOUR_H
            p.setPen(QPen(QColor("#e03131"), 2))
            p.drawLine(QPointF(self.GUTTER + i * w, y), QPointF(self.GUTTER + (i + 1) * w, y))
            p.setBrush(QColor("#e03131"))
            p.drawEllipse(QPointF(self.GUTTER + i * w, y), 4, 4)

    def _event_at(self, pos) -> Ev | None:
        for rect, ev in reversed(self._boxes):
            if rect.contains(pos):
                return ev
        return None

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position()
        ev = self._event_at(pos)
        if ev is not None:
            self.selected, self.selection = ev.id, None
            self.eventClicked.emit(ev.id)
            self.update()
            return
        at = self._time_at(pos)
        if at:
            self._drag_from = at[1]
            self.selection = (at[1], at[1] + dt.timedelta(minutes=30))
            self.selected = None
            self.eventClicked.emit("")
            self.update()

    def mouseMoveEvent(self, event):
        if self._drag_from is None:
            return
        at = self._time_at(event.position())
        if at:
            a, b = sorted((self._drag_from, at[1]))
            self.selection = (a, b + dt.timedelta(minutes=30))
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag_from = None

    def mouseDoubleClickEvent(self, event):
        ev = self._event_at(event.position())
        if ev is not None:
            self.eventActivated.emit(ev.id)
        elif self.selection:
            self.slotActivated.emit(*self.selection)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.selected:
                self.eventActivated.emit(self.selected)
            elif self.selection:
                self.slotActivated.emit(*self.selection)
            return
        super().keyPressEvent(event)

    def scroll_y_for(self, hour: int) -> int:
        return hour * self.HOUR_H


class DayHeader(QWidget):
    """Day names over the grid, and the all-day events as bars across their days."""
    eventClicked = Signal(str)
    eventActivated = Signal(str)
    dayClicked = Signal(object)            # date: open it in Day view
    allDayActivated = Signal(object)       # date: new all-day event

    NAME_H, LANE_H = 30, 22

    def __init__(self, grid: TimeGrid):
        super().__init__()
        self.grid = grid
        self.days: list[dt.date] = []
        self.events: list[Ev] = []
        self.selected: str | None = None
        self.right_margin = 0  # the grid's scrollbar
        self._boxes: list[tuple[QRectF, Ev]] = []
        self._lanes = 0

    def set_content(self, days, events):
        self.days = days
        self.events = sorted([e for e in events if e.all_day],
                             key=lambda e: (e.start, -(e.end - e.start).days))
        lanes: list[dt.datetime] = []
        self._placed = []
        for ev in self.events:
            lane = next((i for i, end in enumerate(lanes) if end <= ev.start), None)
            if lane is None:
                lane = len(lanes)
                lanes.append(ev.end)
            else:
                lanes[lane] = ev.end
            self._placed.append((lane, ev))
        self._lanes = len(lanes)
        self.setFixedHeight(self.NAME_H + max(self._lanes, 1) * self.LANE_H + 6)
        self.update()

    def _col_w(self) -> float:
        return (self.width() - TimeGrid.GUTTER - self.right_margin) / max(len(self.days), 1)

    def paintEvent(self, _):
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Window))
        w = self._col_w()
        today = dt.date.today()
        loc = QLocale.system()
        accent = pal.color(QPalette.ColorRole.Highlight)
        for i, day in enumerate(self.days):
            x = TimeGrid.GUTTER + i * w
            name = loc.dayName(day.isoweekday(), QLocale.FormatType.ShortFormat)
            font = QFont(p.font())
            font.setBold(day == today)
            p.setFont(font)
            p.setPen(accent if day == today else pal.color(QPalette.ColorRole.WindowText))
            p.drawText(QRectF(x + 6, 4, w - 8, self.NAME_H - 6),
                       Qt.AlignmentFlag.AlignVCenter, f"{name} {day.day}")
        p.setFont(QFont())
        self._boxes = []
        first, last = (self.days[0], self.days[-1] + dt.timedelta(days=1)) if self.days else \
            (None, None)
        for lane, ev in self._placed:
            if not self.days:
                break
            s = max(ev.start.date(), first)
            e = min(ev.end.date(), last)
            if e <= s:
                continue
            i1, i2 = (s - first).days, (e - first).days
            rect = QRectF(TimeGrid.GUTTER + i1 * w + 2, self.NAME_H + lane * self.LANE_H,
                          (i2 - i1) * w - 4, self.LANE_H - 3)
            draw_event(p, rect, ev, pal, ev.id == self.selected, [ev.subject])
            self._boxes.append((rect, ev))
        line = QColor(pal.color(QPalette.ColorRole.PlaceholderText))
        line.setAlpha(70)
        p.setPen(line)
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def _day_at(self, x: float) -> dt.date | None:
        if x < TimeGrid.GUTTER or not self.days:
            return None
        i = int((x - TimeGrid.GUTTER) // self._col_w())
        return self.days[i] if 0 <= i < len(self.days) else None

    def mousePressEvent(self, event):
        pos = event.position()
        for rect, ev in self._boxes:
            if rect.contains(pos):
                self.eventClicked.emit(ev.id)
                return
        if pos.y() < self.NAME_H:
            day = self._day_at(pos.x())
            if day:
                self.dayClicked.emit(day)

    def mouseDoubleClickEvent(self, event):
        pos = event.position()
        for rect, ev in self._boxes:
            if rect.contains(pos):
                self.eventActivated.emit(ev.id)
                return
        day = self._day_at(pos.x())
        if day and pos.y() >= self.NAME_H:
            self.allDayActivated.emit(day)


class MonthGrid(QWidget):
    eventClicked = Signal(str)
    eventActivated = Signal(str)
    dayClicked = Signal(object)
    allDayActivated = Signal(object)

    CHIP_H, HEAD_H = 18, 22

    def __init__(self):
        super().__init__()
        self.days: list[dt.date] = []
        self.month = dt.date.today().month
        self.events: list[Ev] = []
        self.selected: str | None = None
        self._boxes: list[tuple[QRectF, Ev | None, dt.date]] = []

    def set_content(self, days, events, month):
        self.days, self.events, self.month = days, events, month
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Base)
        dim = pal.color(QPalette.ColorRole.PlaceholderText)
        accent = pal.color(QPalette.ColorRole.Highlight)
        p.fillRect(self.rect(), base)
        loc = QLocale.system()
        names_h = 22
        cw, ch = self.width() / 7, (self.height() - names_h) / 6
        for i in range(7):
            day = self.days[i] if self.days else dt.date.today()
            p.setPen(dim)
            p.drawText(QRectF(i * cw + 6, 0, cw, names_h), Qt.AlignmentFlag.AlignVCenter,
                       loc.dayName(day.isoweekday(), QLocale.FormatType.ShortFormat))
        line = QColor(dim)
        line.setAlpha(70)
        self._boxes = []
        by_day: dict[dt.date, list[Ev]] = {}
        for ev in sorted(self.events, key=lambda e: (not e.all_day, e.start)):
            d = ev.start.date()
            last = (ev.end - dt.timedelta(seconds=1)).date()
            while d <= last:
                by_day.setdefault(d, []).append(ev)
                d += dt.timedelta(days=1)
        today = dt.date.today()
        for n, day in enumerate(self.days):
            r, c = divmod(n, 7)
            cell = QRectF(c * cw, names_h + r * ch, cw, ch)
            if day.month != self.month:
                p.fillRect(cell, pal.color(QPalette.ColorRole.AlternateBase))
            p.setPen(line)
            p.drawRect(cell)
            font = QFont(p.font())
            font.setBold(day == today)
            p.setFont(font)
            p.setPen(accent if day == today else (pal.color(QPalette.ColorRole.Text)
                                                  if day.month == self.month else dim))
            p.drawText(cell.adjusted(6, 2, -4, 0), Qt.AlignmentFlag.AlignLeft, str(day.day))
            p.setFont(QFont())
            items = by_day.get(day, [])
            room = max(int((ch - self.HEAD_H - 2) // self.CHIP_H), 0)
            shown = items if len(items) <= room else items[:max(room - 1, 0)]
            for k, ev in enumerate(shown):
                rect = QRectF(cell.left() + 3, cell.top() + self.HEAD_H + k * self.CHIP_H,
                              cw - 6, self.CHIP_H - 2)
                label = ev.subject if ev.all_day else f"{fmt_time(ev.start)} {ev.subject}"
                draw_event(p, rect, ev, pal, ev.id == self.selected, [label])
                self._boxes.append((rect, ev, day))
            if len(shown) < len(items):
                rect = QRectF(cell.left() + 6, cell.top() + self.HEAD_H + len(shown) *
                              self.CHIP_H, cw - 8, self.CHIP_H)
                p.setPen(accent)
                p.drawText(rect, Qt.AlignmentFlag.AlignVCenter,
                           f"+{len(items) - len(shown)} more")
                self._boxes.append((rect, None, day))
            self._boxes.append((QRectF(cell.left(), cell.top(), cell.width(), self.HEAD_H),
                                None, day))

    def _hit(self, pos):
        for rect, ev, day in self._boxes:
            if rect.contains(pos):
                return ev, day, rect.height() == self.HEAD_H or ev is None
        if not self.days:
            return None, None, False
        names_h = 22
        cw, ch = self.width() / 7, (self.height() - names_h) / 6
        r, c = int((pos.y() - names_h) // ch), int(pos.x() // cw)
        n = r * 7 + c
        return None, (self.days[n] if 0 <= n < len(self.days) else None), False

    def mousePressEvent(self, event):
        ev, day, header = self._hit(event.position())
        if ev is not None:
            self.eventClicked.emit(ev.id)
        elif header and day:
            self.dayClicked.emit(day)
        else:
            self.eventClicked.emit("")

    def mouseDoubleClickEvent(self, event):
        ev, day, _ = self._hit(event.position())
        if ev is not None:
            self.eventActivated.emit(ev.id)
        elif day:
            self.allDayActivated.emit(day)


class CalendarPage(QWidget):
    rangeChanged = Signal(object, object)          # first day, last day (exclusive)
    newEventRequested = Signal(object, object, bool)  # start, end, all day
    editRequested = Signal(str)
    deleteRequested = Signal(str)
    respondRequested = Signal(str, str)            # event id, accept/tentativelyAccept/decline
    refreshRequested = Signal()
    calendarToggled = Signal(str, bool)

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.mode = WORK_WEEK
        self.anchor = dt.date.today()
        self.selected: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QToolBar("Calendar")
        bar.setIconSize(QSize(16, 16))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.act_new = bar.addAction(iconset.load("calendar-plus"), "New event", self._new)
        bar.addSeparator()
        bar.addAction("Today", self.go_today)
        self.act_prev = bar.addAction(iconset.load("chevron-left"), "", lambda: self.step(-1))
        self.act_prev.setToolTip("Previous")
        self.act_next = bar.addAction(iconset.load("chevron-right"), "", lambda: self.step(1))
        self.act_next.setToolTip("Next")
        self.title = QLabel()
        font = QFont(self.title.font())
        font.setPointSizeF(font.pointSizeF() * 1.2)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setContentsMargins(8, 0, 8, 0)
        bar.addWidget(self.title)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.mode_buttons = QButtonGroup(self)
        for mode, label in ((DAY, "Day"), (WORK_WEEK, "Work week"), (WEEK, "Week"),
                            (MONTH, "Month")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setChecked(mode == self.mode)
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            self.mode_buttons.addButton(b)
            bar.addWidget(b)
        bar.addSeparator()
        bar.addAction(iconset.load("refresh-cw"), "Refresh", self.refreshRequested.emit)
        lay.addWidget(bar)

        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(4, 4, 4, 4)
        self.mini = QCalendarWidget()
        self.mini.setGridVisible(False)
        self.mini.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.mini.setFirstDayOfWeek(QLocale.system().firstDayOfWeek())
        self.mini.clicked.connect(lambda d: self.go_to(d.toPython()))
        llay.addWidget(self.mini)
        llay.addWidget(QLabel("<b>Calendars</b>"))
        self.cal_list = QListWidget()
        self.cal_list.itemChanged.connect(self._calendar_checked)
        llay.addWidget(self.cal_list, 1)

        self.grid = TimeGrid()
        self.header = DayHeader(self.grid)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.grid)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        time_view = QWidget()
        tlay = QVBoxLayout(time_view)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(0)
        tlay.addWidget(self.header)
        tlay.addWidget(self.scroll, 1)
        self.month_grid = MonthGrid()
        self.stack = QStackedWidget()
        self.stack.addWidget(time_view)
        self.stack.addWidget(self.month_grid)
        for view in (self.grid, self.header, self.month_grid):
            view.eventClicked.connect(self.select_event)
            view.eventActivated.connect(self.editRequested)
        self.grid.slotActivated.connect(lambda s, e: self.newEventRequested.emit(s, e, False))
        for view in (self.header, self.month_grid):
            view.dayClicked.connect(lambda d: (self.go_to(d), self.set_mode(DAY)))
            view.allDayActivated.connect(lambda d: self.newEventRequested.emit(
                local_midnight(d), local_midnight(d + dt.timedelta(days=1)), True))

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        rlay.addWidget(self.details, 1)
        buttons = QHBoxLayout()
        self.join = QPushButton(iconset.load("video"), "Join Teams")
        self.join.clicked.connect(self._join)
        self.accept_b = QPushButton(iconset.load("check"), "Accept")
        self.tentative_b = QPushButton(iconset.load("circle-help"), "Tentative")
        self.decline_b = QPushButton(iconset.load("x"), "Decline")
        for b, resp in ((self.accept_b, "accept"), (self.tentative_b, "tentativelyAccept"),
                        (self.decline_b, "decline")):
            b.clicked.connect(lambda _=False, r=resp: self.selected and
                              self.respondRequested.emit(self.selected, r))
        self.edit_b = QPushButton(iconset.load("pencil"), "Edit")
        self.edit_b.clicked.connect(lambda: self.selected and self.editRequested.emit(
            self.selected))
        self.delete_b = QPushButton(iconset.load("trash-2"), "Delete")
        self.delete_b.clicked.connect(lambda: self.selected and self.deleteRequested.emit(
            self.selected))
        respond = QHBoxLayout()
        for b in (self.accept_b, self.tentative_b, self.decline_b):
            respond.addWidget(b)
        rlay.addLayout(respond)
        for b in (self.join, self.edit_b, self.delete_b):
            buttons.addWidget(b)
        rlay.addLayout(buttons)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.stack)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 760, 300])
        lay.addWidget(split, 1)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.grid.scroll_y_for(7)))
        self.reload_calendars()
        self._apply_range(announce=False)

    # -- the period shown ------------------------------------------------------------------
    def days(self) -> list[dt.date]:
        if self.mode == DAY:
            return [self.anchor]
        if self.mode == MONTH:
            first = week_start(self.anchor.replace(day=1))
            return [first + dt.timedelta(days=i) for i in range(42)]
        start = week_start(self.anchor)
        days = [start + dt.timedelta(days=i) for i in range(7)]
        return [d for d in days if d.isoweekday() < 6] if self.mode == WORK_WEEK else days

    def period(self) -> tuple[dt.date, dt.date]:
        days = self.days()
        return days[0], days[-1] + dt.timedelta(days=1)

    def set_mode(self, mode: str):
        self.mode = mode
        for b, m in zip(self.mode_buttons.buttons(), (DAY, WORK_WEEK, WEEK, MONTH)):
            b.setChecked(m == mode)
        self._apply_range()

    def go_to(self, day: dt.date):
        self.anchor = day
        self._apply_range()

    def go_today(self):
        self.go_to(dt.date.today())

    def step(self, direction: int):
        if self.mode == DAY:
            self.anchor += dt.timedelta(days=direction)
        elif self.mode == MONTH:
            m = self.anchor.month - 1 + direction
            self.anchor = dt.date(self.anchor.year + m // 12, m % 12 + 1, 1)
        else:
            self.anchor += dt.timedelta(weeks=direction)
        self._apply_range()

    def _apply_range(self, announce: bool = True):
        days = self.days()
        loc = QLocale.system()
        if self.mode == MONTH:
            self.title.setText(f"{loc.monthName(self.anchor.month)} {self.anchor.year}")
        elif self.mode == DAY:
            self.title.setText(loc.toString(qdate(self.anchor), QLocale.FormatType.LongFormat))
        else:
            a, b = days[0], days[-1]
            self.title.setText(f"{loc.toString(qdate(a), 'd MMM')} – "
                               f"{loc.toString(qdate(b), 'd MMM yyyy')}")
        self.mini.setSelectedDate(qdate(self.anchor))
        self.stack.setCurrentWidget(self.month_grid if self.mode == MONTH
                                    else self.stack.widget(0))
        self.reload()
        if announce:
            self.rangeChanged.emit(*self.period())

    # -- data ------------------------------------------------------------------------
    def reload_calendars(self):
        self.cal_list.blockSignals(True)
        self.cal_list.clear()
        for cal in self.store.calendar_rows():
            item = QListWidgetItem(color_icon(cal["color"] or DEFAULT_CALENDAR_COLOR, 14),
                                   cal["name"])
            item.setData(Qt.ItemDataRole.UserRole, cal["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if cal["visible"]
                               else Qt.CheckState.Unchecked)
            if cal["owner"]:
                item.setToolTip(cal["owner"])
            self.cal_list.addItem(item)
        self.cal_list.blockSignals(False)

    def _calendar_checked(self, item: QListWidgetItem):
        self.calendarToggled.emit(item.data(Qt.ItemDataRole.UserRole),
                                  item.checkState() == Qt.CheckState.Checked)
        self.reload()

    def reload(self):
        """Redraw from the store (after a fetch or a change)."""
        first, last = self.period()
        colors = self.store.category_colors()
        events = [Ev(r, colors) for r in self.store.events_between(*period(first, last))]
        days = self.days()
        for view in (self.grid, self.header, self.month_grid):
            view.selected = self.selected
        if self.mode == MONTH:
            self.month_grid.set_content(days, events, self.anchor.month)
        else:
            self.header.right_margin = self.scroll.verticalScrollBar().sizeHint().width()
            self.grid.set_content(days, events)
            self.header.set_content(days, events)
        if self.selected and not self.store.event_row(self.selected):
            self.selected = None
        self._show_details()

    def select_event(self, event_id: str):
        self.selected = event_id or None
        for view in (self.grid, self.header, self.month_grid):
            view.selected = self.selected
            view.update()
        self._show_details()

    # -- details --------------------------------------------------------------------------
    def _show_details(self):
        row = self.store.event_row(self.selected) if self.selected else None
        is_meeting = bool(row and json.loads(row["attendees"] or "[]"))
        invited = bool(row and is_meeting and not row["is_organizer"])
        for b in (self.accept_b, self.tentative_b, self.decline_b):
            b.setVisible(invited)
        self.join.setVisible(bool(row and row["online_url"]))
        self.edit_b.setEnabled(row is not None)
        self.delete_b.setEnabled(row is not None)
        self.delete_b.setText("Cancel meeting" if row and is_meeting and row["is_organizer"]
                              else "Delete")
        if row is None:
            self.details.setHtml("<p style='color:gray'>Select an event to see it here. "
                                 "Double-click an empty time to make one.</p>")
            return
        e = html.escape
        start, end = event_span(row)
        loc = QLocale.system()
        if row["all_day"]:
            last = end - dt.timedelta(days=1)
            when = loc.toString(qdate(start.date()), QLocale.FormatType.LongFormat)
            if last.date() != start.date():
                when += " – " + loc.toString(qdate(last.date()), QLocale.FormatType.LongFormat)
            when += " (all day)"
        else:
            when = (f"{loc.toString(qdate(start.date()), QLocale.FormatType.LongFormat)}, "
                    f"{fmt_time(start)} – {fmt_time(end)}")
            if end.date() != start.date():
                when += f" ({loc.toString(qdate(end.date()), QLocale.FormatType.ShortFormat)})"
        colors = self.store.category_colors()
        cats = "".join(
            f"<span style='background:{colors.get(c, '#8e9aaf')};color:white;"
            f"border-radius:3px'>&nbsp;{e(c)}&nbsp;</span> "
            for c in json.loads(row["categories"] or "[]"))
        parts = [f"<p><span style='font-size:large;font-weight:bold'>"
                 f"{'<s>' if row['cancelled'] else ''}{e(row['subject'] or '(no title)')}"
                 f"{'</s>' if row['cancelled'] else ''}</span><br>{e(when)}</p>"]
        if row["cancelled"]:
            parts.append("<p style='color:#e03131'>This meeting was cancelled.</p>")
        if row["location"]:
            parts.append(f"<p>📍 {e(row['location'])}</p>")
        if row["online_url"]:
            parts.append(f"<p><a href='{e(row['online_url'])}'>Join Teams meeting</a></p>")
        parts.append(f"<p>Show as: {e(SHOW_AS_LABEL.get(row['show_as'], row['show_as']))}"
                     + (" · Private" if row["sensitivity"] == "private" else "")
                     + (f"<br>{cats}" if cats else "") + "</p>")
        attendees = json.loads(row["attendees"] or "[]")
        if attendees:
            people = [f"<b>{e(row['organizer_name'] or row['organizer_addr'])}</b> (organizer)"]
            people += [f"{e(n or a)}{' (optional)' if kind == 'optional' else ''} — "
                       f"<span style='color:gray'>{e(RESPONSES.get(resp, resp))}</span>"
                       for n, a, kind, resp in attendees]
            parts.append("<p>" + "<br>".join(people) + "</p>")
            if not row["is_organizer"]:
                parts.append(f"<p>Your response: "
                             f"{e(RESPONSES.get(row['response'], row['response'] or '—'))}</p>")
        if row["preview"]:
            parts.append(f"<p style='color:gray'>{e(row['preview'])}</p>")
        self.details.setHtml("".join(parts))

    def _join(self):
        row = self.store.event_row(self.selected) if self.selected else None
        if row and row["online_url"]:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(row["online_url"]))

    def _new(self):
        if self.mode != MONTH and self.grid.selection:
            self.newEventRequested.emit(*self.grid.selection, False)
        else:
            day = self.anchor
            start = dt.datetime.combine(day, dt.time(9)).astimezone()
            if day == dt.date.today():
                now = dt.datetime.now().astimezone().replace(second=0, microsecond=0)
                start = now + dt.timedelta(minutes=30 - now.minute % 30)
            self.newEventRequested.emit(start, start + dt.timedelta(minutes=30), False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and self.selected:
            self.deleteRequested.emit(self.selected)
            return
        super().keyPressEvent(event)
