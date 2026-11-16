"""Calendar: calendars, events, categories, free/busy (no Qt imports; unit-testable).

Events are fetched per visible period with calendarView (which expands recurring series into
occurrences) and cached; the views read the cache. Times travel in UTC: timed events are
stored as UTC ISO strings and shown in local time; all-day events are dates (end exclusive),
the same day everywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from .graph import GraphClient, GraphError
from .store import Store

log = logging.getLogger(__name__)

# Show as, in Outlook's order. ("Available" in some clients is Free.)
SHOW_AS = [("free", "Free"), ("workingElsewhere", "Working elsewhere"),
           ("tentative", "Tentative"), ("busy", "Busy"), ("oof", "Out of office")]
SHOW_AS_LABEL = dict(SHOW_AS)

# Outlook's category colour presets.
PRESET_COLORS = {
    "preset0": "#e74856", "preset1": "#ff8c00", "preset2": "#ab6b2e", "preset3": "#ffd43b",
    "preset4": "#47d041", "preset5": "#30c6cc", "preset6": "#73aa24", "preset7": "#4f8cff",
    "preset8": "#8764b8", "preset9": "#e43ba6", "preset10": "#8e9aaf", "preset11": "#4a6178",
    "preset12": "#a0a0a0", "preset13": "#5c5c5c", "preset14": "#222222", "preset15": "#a4262c",
    "preset16": "#ca5010", "preset17": "#8e562e", "preset18": "#986f0b", "preset19": "#0b6a0b",
    "preset20": "#038387", "preset21": "#5b7a1b", "preset22": "#004e8c", "preset23": "#5c2e91",
    "preset24": "#b4009e",
}
DEFAULT_CALENDAR_COLOR = "#4f8cff"
REMINDERS = [(None, "None"), (0, "At start time"), (5, "5 minutes before"),
             (15, "15 minutes before"), (30, "30 minutes before"), (60, "1 hour before"),
             (120, "2 hours before"), (1440, "1 day before"), (10080, "1 week before")]


# -- time ----------------------------------------------------------------------------
def utc_iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> dt.datetime:
    """'2026-11-16T15:00:00Z' or Graph's '2026-11-16T15:00:00.0000000' (UTC) -> aware."""
    return dt.datetime.fromisoformat(s[:19]).replace(tzinfo=dt.timezone.utc)


def local_midnight(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day).astimezone()


def period(first_day: dt.date, last_day: dt.date) -> tuple[str, str, str, str]:
    """(start_utc, end_utc, first_day, last_day) for the local days [first_day, last_day)."""
    return (utc_iso(local_midnight(first_day)), utc_iso(local_midnight(last_day)),
            first_day.isoformat(), last_day.isoformat())


def event_span(row) -> tuple[dt.datetime, dt.datetime]:
    """Local start and end of a stored event (all-day: local midnights)."""
    if row["all_day"]:
        return (local_midnight(dt.date.fromisoformat(row["start"])),
                local_midnight(dt.date.fromisoformat(row["end"])))
    return parse_utc(row["start"]).astimezone(), parse_utc(row["end"]).astimezone()


# -- Graph <-> rows ----------------------------------------------------------------------
def calendar_row(item: dict) -> dict:
    owner = (item.get("owner") or {})
    return {
        "id": item["id"], "name": item.get("name") or "Calendar",
        "color": item.get("hexColor") or DEFAULT_CALENDAR_COLOR,
        "is_default": int(bool(item.get("isDefaultCalendar"))),
        "can_edit": int(item.get("canEdit", True)),
        "teams": int("teamsForBusiness" in (item.get("allowedOnlineMeetingProviders") or [])),
        "owner": owner.get("address"),
    }


def event_row(item: dict, account_id: str, calendar_id: str) -> dict:
    all_day = bool(item.get("isAllDay"))
    start = (item.get("start") or {}).get("dateTime") or ""
    end = (item.get("end") or {}).get("dateTime") or start
    if all_day:  # midnight to midnight: keep the dates
        start, end = start[:10], end[:10]
        if end <= start:
            end = (dt.date.fromisoformat(start) + dt.timedelta(days=1)).isoformat()
    else:
        start, end = utc_iso(parse_utc(start)), utc_iso(parse_utc(end))
    org = ((item.get("organizer") or {}).get("emailAddress") or {})
    attendees = [[(a.get("emailAddress") or {}).get("name") or "",
                  (a.get("emailAddress") or {}).get("address") or "",
                  a.get("type") or "required",
                  (a.get("status") or {}).get("response") or "none"]
                 for a in item.get("attendees") or []]
    online = item.get("onlineMeeting") or {}
    return {
        "id": item["id"], "account_id": account_id, "calendar_id": calendar_id,
        "subject": item.get("subject") or "", "start": start, "end": end,
        "all_day": int(all_day), "show_as": item.get("showAs") or "busy",
        "categories": json.dumps(item.get("categories") or []),
        "location": (item.get("location") or {}).get("displayName") or "",
        "organizer_name": org.get("name") or "", "organizer_addr": org.get("address") or "",
        "is_organizer": int(bool(item.get("isOrganizer"))),
        "attendees": json.dumps(attendees),
        "online_url": online.get("joinUrl") or "",
        "preview": item.get("bodyPreview") or "",
        "response": (item.get("responseStatus") or {}).get("response") or "",
        "cancelled": int(bool(item.get("isCancelled"))),
        "kind": item.get("type") or "singleInstance",
        "series_id": item.get("seriesMasterId"),
        "sensitivity": item.get("sensitivity") or "normal",
        "reminder": item.get("reminderMinutesBeforeStart") if item.get("isReminderOn") else None,
    }


# -- recurrence ------------------------------------------------------------------------------
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDINALS = ["first", "second", "third", "fourth", "last"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def ordinal_of(day: dt.date) -> str:
    """Which of its weekday in the month a date is: first..fourth, or last."""
    n = (day.day - 1) // 7
    if (day + dt.timedelta(days=7)).month != day.month:
        return "last"
    return ORDINALS[n]


def presets(day: dt.date) -> list[tuple[str, dict]]:
    """Outlook's "Repeat" choices for an event on `day`: (label, Graph pattern)."""
    wd = WEEKDAYS[day.weekday()]
    nth = ordinal_of(day)
    return [
        ("Daily", {"type": "daily", "interval": 1}),
        ("Every weekday", {"type": "weekly", "interval": 1, "daysOfWeek": WEEKDAYS[:5]}),
        (f"Weekly on {wd.capitalize()}", {"type": "weekly", "interval": 1, "daysOfWeek": [wd]}),
        (f"Monthly on day {day.day}", {"type": "absoluteMonthly", "interval": 1,
                                        "dayOfMonth": day.day}),
        (f"Monthly on the {nth} {wd.capitalize()}",
         {"type": "relativeMonthly", "interval": 1, "daysOfWeek": [wd], "index": nth}),
        (f"Yearly on {MONTHS[day.month - 1]} {day.day}",
         {"type": "absoluteYearly", "interval": 1, "dayOfMonth": day.day, "month": day.month}),
    ]


def make_recurrence(pattern: dict, start: dt.date, end_date: dt.date | None = None,
                    count: int | None = None, tz: str = "UTC") -> dict:
    """A Graph recurrence: the pattern, starting on `start`, ending on a date, after `count`
    occurrences, or never."""
    pattern = {"firstDayOfWeek": "sunday", **pattern}
    if end_date:
        rng = {"type": "endDate", "startDate": start.isoformat(), "endDate": end_date.isoformat()}
    elif count:
        rng = {"type": "numbered", "startDate": start.isoformat(), "numberOfOccurrences": count}
    else:
        rng = {"type": "noEnd", "startDate": start.isoformat()}
    rng["recurrenceTimeZone"] = tz
    return {"pattern": pattern, "range": rng}


def describe_recurrence(rec: dict | None) -> str:
    """"Every 2 weeks on Monday and Wednesday, until 31 Dec 2026" and the like."""
    if not rec:
        return "Does not repeat"
    p, r = rec.get("pattern") or {}, rec.get("range") or {}
    n = p.get("interval") or 1
    kind = p.get("type")
    days = [d.capitalize() for d in p.get("daysOfWeek") or []]
    day_list = (", ".join(days[:-1]) + " and " + days[-1]) if len(days) > 1 else         (days[0] if days else "")
    every = lambda unit: f"Every {unit}" if n == 1 else f"Every {n} {unit}s"  # noqa: E731
    if kind == "daily":
        text = every("day")
    elif kind == "weekly":
        text = ("Every weekday" if n == 1 and [d.lower() for d in days] == WEEKDAYS[:5]
                else f"{every('week')} on {day_list}")
    elif kind == "absoluteMonthly":
        text = f"{every('month')} on day {p.get('dayOfMonth')}"
    elif kind == "relativeMonthly":
        text = f"{every('month')} on the {p.get('index', 'first')} {day_list}"
    elif kind == "absoluteYearly":
        text = f"{every('year')} on {MONTHS[(p.get('month') or 1) - 1]} {p.get('dayOfMonth')}"
    elif kind == "relativeYearly":
        text = (f"{every('year')} on the {p.get('index', 'first')} {day_list} of "
                f"{MONTHS[(p.get('month') or 1) - 1]}")
    else:
        text = "Repeats"
    if r.get("type") == "endDate" and r.get("endDate"):
        text += f", until {r['endDate'][:10]}"
    elif r.get("type") == "numbered":
        text += f", {r.get('numberOfOccurrences')} times"
    return text


@dataclass
class EventDraft:
    """What the editor produces; fields() turns it into a Graph event."""
    subject: str
    start: dt.datetime            # local; for all-day only the date counts
    end: dt.datetime
    all_day: bool = False
    location: str = ""
    required: tuple = ()          # (name, address) pairs
    optional: tuple = ()
    teams: bool = False
    show_as: str = "busy"
    categories: tuple = ()
    reminder: int | None = 15
    body: str | None = None       # None: leave the body as it is
    body_html: bool = False       # body is HTML (with cid: links to `inline`)
    inline: tuple = ()            # InlineImage: pictures in the body, uploaded after saving
    private: bool = False
    recurrence: dict | None = None  # make_recurrence(...): a series
    stop_repeating: bool = False    # editing a series and choosing "Does not repeat"
    tz: str = "UTC"                 # your time zone (Windows name): DST-proof repeating times

    def fields(self) -> dict:
        if self.all_day:
            first = self.start.date()
            last = max(self.end.date(), first + dt.timedelta(days=1))
            when = {"start": {"dateTime": f"{first.isoformat()}T00:00:00", "timeZone": self.tz},
                    "end": {"dateTime": f"{last.isoformat()}T00:00:00", "timeZone": self.tz}}
        elif self.tz == "UTC":
            when = {"start": {"dateTime": utc_iso(self.start).rstrip("Z"), "timeZone": "UTC"},
                    "end": {"dateTime": utc_iso(self.end).rstrip("Z"), "timeZone": "UTC"}}
        else:  # wall-clock time in your zone (start/end are local times)
            fmt = "%Y-%m-%dT%H:%M:%S"
            when = {"start": {"dateTime": self.start.astimezone().strftime(fmt),
                              "timeZone": self.tz},
                    "end": {"dateTime": self.end.astimezone().strftime(fmt), "timeZone": self.tz}}
        out = {
            "subject": self.subject, **when, "isAllDay": self.all_day,
            "location": {"displayName": self.location},
            "attendees": [{"emailAddress": {"name": n or a, "address": a}, "type": kind}
                          for kind, people in (("required", self.required),
                                               ("optional", self.optional))
                          for n, a in people],
            "showAs": self.show_as, "categories": list(self.categories),
            "isReminderOn": self.reminder is not None,
            "sensitivity": "private" if self.private else "normal",
        }
        if self.reminder is not None:
            out["reminderMinutesBeforeStart"] = self.reminder
        if self.teams:
            out["isOnlineMeeting"] = True
            out["onlineMeetingProvider"] = "teamsForBusiness"
        if self.body is not None:
            out["body"] = {"contentType": "html" if self.body_html else "text",
                           "content": self.body}
        if self.recurrence:
            rec = json.loads(json.dumps(self.recurrence))  # (a copy)
            rec["range"]["startDate"] = self.start.date().isoformat()
            rec["range"]["recurrenceTimeZone"] = self.tz
            out["recurrence"] = rec
        elif self.stop_repeating:
            out["recurrence"] = None
        return out


# -- sync ---------------------------------------------------------------------------------
class CalendarSync:
    def __init__(self, store: Store, graph: GraphClient, account_id: str) -> None:
        self.store, self.graph, self.account_id = store, graph, account_id

    def sync_calendars(self) -> None:
        self.store.replace_calendars(self.account_id,
                                     [calendar_row(c) for c in self.graph.calendars()])
        try:
            self.store.replace_categories(self.account_id, [
                (c.get("displayName") or "", PRESET_COLORS.get(c.get("color"), "#8e9aaf"))
                for c in self.graph.categories() if c.get("displayName")])
        except GraphError as exc:  # no MailboxSettings permission: no category colours
            log.info("categories unavailable: %s", exc)

    def fetch(self, first_day: dt.date, last_day: dt.date) -> int:
        """Refresh the cached events of every calendar for the local days
        [first_day, last_day). Returns how many events there are."""
        start_utc, end_utc, d1, d2 = period(first_day, last_day)
        total = 0
        for cal in [c for c in self.store.calendar_rows() if c["account_id"] == self.account_id]:
            try:
                items = self.graph.calendar_view(cal["id"], start_utc, end_utc)
            except GraphError as exc:  # a shared calendar we can't read any more, say
                log.warning("calendar %s: %s", cal["name"], exc)
                continue
            rows = [event_row(i, self.account_id, cal["id"]) for i in items]
            self.store.replace_events(cal["id"], start_utc, end_utc, d1, d2, rows)
            total += len(rows)
        return total


# -- reminders ------------------------------------------------------------------------------
def reminder_time(row) -> dt.datetime | None:
    """When an event's reminder goes off (local), or None if it has none."""
    if row["reminder"] is None or row["cancelled"] or row["response"] == "declined":
        return None
    return event_span(row)[0] - dt.timedelta(minutes=row["reminder"])


def due_reminders(rows, now: dt.datetime, state: dict) -> list:
    """Events whose reminder has gone off and that haven't ended, minus the dismissed and
    the snoozed (state: {(event id, start): (dismissed, snoozed until ISO or None)}),
    soonest first."""
    due = []
    for row in rows:
        fire = reminder_time(row)
        if fire is None or fire > now or event_span(row)[1] <= now:
            continue
        dismissed, until = state.get((row["id"], row["start"]), (False, None))
        if dismissed or (until and parse_utc(until) > now):
            continue
        due.append(row)
    return sorted(due, key=lambda r: event_span(r)[0])


# -- layout and free time ----------------------------------------------------------------
def overlap_columns(spans: list[tuple[dt.datetime, dt.datetime]]) -> list[tuple[int, int]]:
    """Side-by-side placement for one day's timed events: (column, columns) per span, in the
    order given. Events that overlap (directly or through others) share the width."""
    order = sorted(range(len(spans)), key=lambda i: (spans[i][0], -(spans[i][1] - spans[i][0])))
    result: list[tuple[int, int]] = [(0, 1)] * len(spans)
    cluster: list[int] = []
    col_ends: list[dt.datetime] = []
    cluster_end = None

    def close():
        for i in cluster:
            result[i] = (result[i][0], len(col_ends))

    for i in order:
        start, end = spans[i]
        if cluster_end is not None and start >= cluster_end:
            close()
            cluster, col_ends, cluster_end = [], [], None
        col = next((c for c, e in enumerate(col_ends) if e <= start), None)
        if col is None:
            col = len(col_ends)
            col_ends.append(end)
        else:
            col_ends[col] = end
        result[i] = (col, 0)
        cluster.append(i)
        cluster_end = end if cluster_end is None else max(cluster_end, end)
    close()
    return result


BUSY_STATES = {"busy", "oof", "tentative", "workingElsewhere"}


def busy_periods(schedule: dict) -> list[tuple[dt.datetime, dt.datetime, str]]:
    """(start, end, status) of someone's getSchedule entry, in local time."""
    out = []
    for item in schedule.get("scheduleItems") or []:
        status = item.get("status") or "busy"
        out.append((parse_utc(item["start"]["dateTime"]).astimezone(),
                    parse_utc(item["end"]["dateTime"]).astimezone(), status))
    return out


def find_free_time(busy: list[list[tuple[dt.datetime, dt.datetime, str]]],
                   after: dt.datetime, duration: dt.timedelta, day_start: int = 8,
                   day_end: int = 18, days: int = 14, step: int = 30,
                   strict: bool = False) -> dt.datetime | None:
    """The first start at or after `after` (on a `step`-minute grid, weekdays, between
    `day_start` and `day_end` o'clock) when nobody is busy for `duration`. Tentative and
    working-elsewhere time counts as busy only when `strict`."""
    blocking = BUSY_STATES if strict else {"busy", "oof"}
    periods = [(s, e) for person in busy for s, e, st in person if st in blocking]
    t = after.replace(second=0, microsecond=0)
    t += dt.timedelta(minutes=(-t.minute) % step)
    limit = after + dt.timedelta(days=days)
    while t < limit:
        day_open = t.replace(hour=day_start, minute=0)
        day_close = t.replace(hour=day_end, minute=0)
        if t.weekday() >= 5 or t + duration > day_close:
            t = (t + dt.timedelta(days=1)).replace(hour=day_start, minute=0)
            continue
        if t < day_open:
            t = day_open
            continue
        end = t + duration
        clash = [e for s, e in periods if s < end and e > t]
        if not clash:
            return t
        t = max(clash)  # skip past the clash, back onto the grid
        t += dt.timedelta(minutes=(-t.minute) % step)
    return None
