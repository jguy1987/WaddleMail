"""Calendar: event rows, drafts for Graph, layout of overlapping events, free time."""
from __future__ import annotations

import datetime as dt
import json

from waddlemail.calendars import (
    CalendarSync, EventDraft, calendar_row, event_row, event_span, find_free_time,
    overlap_columns, period, utc_iso,
)
from waddlemail.graph import GraphClient
from waddlemail.store import Store
from tests.test_mailboxes import FakeResponse

LOCAL = dt.datetime.now().astimezone().tzinfo


def local(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=LOCAL)


def graph_event(eid, start, end, all_day=False, **kw):
    return {"id": eid, "subject": eid, "isAllDay": all_day,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"}, **kw}


def test_event_rows():
    r = event_row(graph_event("e1", "2026-11-16T15:00:00.0000000", "2026-11-16T16:00:00.0000000",
                              showAs="oof", categories=["Red category"],
                              isOnlineMeeting=True,
                              onlineMeeting={"joinUrl": "https://teams.microsoft.com/l/x"},
                              attendees=[{"emailAddress": {"name": "Ana", "address": "a@x"},
                                          "type": "required",
                                          "status": {"response": "accepted"}}],
                              isReminderOn=True, reminderMinutesBeforeStart=15), "acct", "cal")
    assert (r["start"], r["end"], r["show_as"]) == ("2026-11-16T15:00:00Z",
                                                    "2026-11-16T16:00:00Z", "oof")
    assert r["online_url"].startswith("https://teams") and r["reminder"] == 15
    assert json.loads(r["attendees"]) == [["Ana", "a@x", "required", "accepted"]]
    day = event_row(graph_event("e2", "2026-11-20T00:00:00", "2026-11-22T00:00:00", True),
                    "acct", "cal")
    assert (day["start"], day["end"], day["all_day"]) == ("2026-11-20", "2026-11-22", 1)
    assert calendar_row({"id": "c", "name": "Work", "allowedOnlineMeetingProviders":
                         ["teamsForBusiness"]})["teams"] == 1


def test_drafts_become_graph_events():
    d = EventDraft("Planning", local(2026, 11, 16, 9), local(2026, 11, 16, 10),
                   required=(("Ana", "a@x"),), optional=(("", "b@y"),), teams=True,
                   show_as="workingElsewhere", categories=("Blue",), reminder=None, body="Agenda")
    f = d.fields()
    assert f["start"]["timeZone"] == "UTC" and f["start"]["dateTime"] == \
        utc_iso(local(2026, 11, 16, 9)).rstrip("Z")
    assert [a["type"] for a in f["attendees"]] == ["required", "optional"]
    assert f["isOnlineMeeting"] and f["onlineMeetingProvider"] == "teamsForBusiness"
    assert f["isReminderOn"] is False and "reminderMinutesBeforeStart" not in f
    assert f["body"] == {"contentType": "text", "content": "Agenda"}
    allday = EventDraft("Holiday", local(2026, 12, 24), local(2026, 12, 24), all_day=True,
                        body=None).fields()
    assert (allday["start"]["dateTime"], allday["end"]["dateTime"]) == (
        "2026-12-24T00:00:00", "2026-12-25T00:00:00")  # at least one whole day
    assert allday["isAllDay"] and "body" not in allday
    rich = EventDraft("Visit", local(2026, 11, 16, 9), local(2026, 11, 16, 10),
                      body='<p>Map <img src="cid:image-1@waddlemail"></p>', body_html=True,
                      inline=("picture",)).fields()
    assert rich["body"]["contentType"] == "html" and "inline" not in rich


def test_overlap_columns():
    t = lambda h, m=0: local(2026, 11, 16, h, m)  # noqa: E731
    spans = [(t(9), t(10)), (t(9, 30), t(11)), (t(10), t(10, 30)), (t(13), t(14))]
    # 9-10 and 9:30-11 overlap; 10-10:30 fits back in column 0; 13-14 stands alone
    assert overlap_columns(spans) == [(0, 2), (1, 2), (0, 2), (0, 1)]


def test_find_free_time():
    t = lambda d, h, m=0: local(2026, 11, d, h, m)  # noqa: E731  (16 Nov 2026: a Monday)
    ana = [(t(16, 9), t(16, 11), "busy")]
    bo = [(t(16, 11), t(16, 12), "oof"), (t(16, 12), t(16, 13), "tentative")]
    hour = dt.timedelta(hours=1)
    assert find_free_time([ana, bo], t(16, 9), hour) == t(16, 12)          # tentative is OK
    assert find_free_time([ana, bo], t(16, 9), hour, strict=True) == t(16, 13)
    assert find_free_time([ana], t(16, 17, 30), hour) == t(17, 8)          # next working day
    assert find_free_time([ana], t(20, 17), hour) == t(20, 17)             # ends at 18:00: fits
    assert find_free_time([ana], t(20, 17, 30), hour) == t(23, 8)          # Fri -> Mon


def test_fetch_replaces_the_period(tmp_path):
    st = Store(tmp_path / "m.db")

    class G:
        def __init__(self, items):
            self.items = items

        def calendars(self):
            return [{"id": "cal", "name": "Calendar", "isDefaultCalendar": True}]

        def categories(self):
            return [{"displayName": "Red category", "color": "preset0"}]

        def calendar_view(self, cid, start, end):
            self.asked = (start, end)
            return self.items

    first = utc_iso(local(2026, 11, 16, 9))[:19]
    g = G([graph_event("e1", first, first), graph_event("e2", "2026-11-18T00:00:00",
                                                        "2026-11-19T00:00:00", True)])
    sync = CalendarSync(st, g, "acct")
    sync.sync_calendars()
    assert st.category_colors() == {"Red category": "#e74856"}
    assert sync.fetch(dt.date(2026, 11, 16), dt.date(2026, 11, 23)) == 2
    g.items = []  # e1 and e2 were deleted on the server
    sync.fetch(dt.date(2026, 11, 16), dt.date(2026, 11, 23))
    assert st.events_between(*period(dt.date(2026, 11, 1), dt.date(2026, 12, 1))) == []
    st.set_calendar_visible("cal", False)
    g.items = [graph_event("e3", first, first)]
    sync.fetch(dt.date(2026, 11, 16), dt.date(2026, 11, 17))
    assert st.events_between(*period(dt.date(2026, 11, 1), dt.date(2026, 12, 1))) == []
    sync.sync_calendars()  # hidden stays hidden
    assert st.calendar_row("cal")["visible"] == 0
    row = st.event_row("e3")
    assert event_span(row)[0] == local(2026, 11, 16, 9)


def test_schedule_and_respond_requests():
    class S:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, headers=None, json=None, **kw):
            self.calls.append((method, url.split("v1.0")[1], json))
            return FakeResponse(200, {"value": [{"scheduleId": "a@x", "scheduleItems": []}]})

    s = S()
    g = GraphClient(lambda: "t", session=s)
    assert g.schedule(["a@x"], "2026-11-16T08:00:00Z", "2026-11-16T18:00:00Z")[0][
        "scheduleId"] == "a@x"
    g.respond_event("E", "tentativelyAccept", "maybe")
    body = s.calls[0][2]
    assert s.calls[0][:2] == ("POST", "/me/calendar/getSchedule")
    assert body["startTime"] == {"dateTime": "2026-11-16T08:00:00", "timeZone": "UTC"}
    assert s.calls[1] == ("POST", "/me/events/E/tentativelyAccept",
                          {"comment": "maybe", "sendResponse": True})


def test_repeat_presets_and_wording():
    from waddlemail.calendars import describe_recurrence, make_recurrence, ordinal_of, presets
    monday = dt.date(2026, 11, 30)  # the last Monday of November
    assert ordinal_of(monday) == "last" and ordinal_of(dt.date(2026, 11, 16)) == "third"
    labels = [label for label, _ in presets(monday)]
    assert labels == ["Daily", "Every weekday", "Weekly on Monday", "Monthly on day 30",
                      "Monthly on the last Monday", "Yearly on November 30"]
    weekly = presets(monday)[2][1]
    rec = make_recurrence(weekly, monday, count=10, tz="Central Standard Time")
    assert rec["range"] == {"type": "numbered", "startDate": "2026-11-30",
                            "numberOfOccurrences": 10,
                            "recurrenceTimeZone": "Central Standard Time"}
    assert describe_recurrence(rec) == "Every week on Monday, 10 times"
    two = make_recurrence({"type": "weekly", "interval": 2, "daysOfWeek": ["monday",
                                                                           "wednesday"]},
                          monday, end_date=dt.date(2026, 12, 31))
    assert describe_recurrence(two) == "Every 2 weeks on Monday and Wednesday, until 2026-12-31"
    assert describe_recurrence(make_recurrence(presets(monday)[1][1], monday)) == \
        "Every weekday"
    assert describe_recurrence(None) == "Does not repeat"


def test_drafts_in_a_time_zone_and_series():
    from waddlemail.calendars import make_recurrence, presets
    start = local(2026, 11, 16, 9)
    rec = make_recurrence(presets(start.date())[2][1], dt.date(2000, 1, 1))
    f = EventDraft("Standup", start, start + dt.timedelta(minutes=15), recurrence=rec,
                   tz="Central Standard Time").fields()
    assert f["start"] == {"dateTime": "2026-11-16T09:00:00", "timeZone": "Central Standard Time"}
    assert f["recurrence"]["range"]["startDate"] == "2026-11-16"  # follows the event
    assert f["recurrence"]["range"]["recurrenceTimeZone"] == "Central Standard Time"
    stop = EventDraft("x", start, start, stop_repeating=True).fields()
    assert "recurrence" in stop and stop["recurrence"] is None


def test_due_reminders(tmp_path):
    from waddlemail.calendars import due_reminders
    now = local(2026, 11, 16, 8, 50)

    def ev(eid, h, reminder=15, **kw):
        return {"id": eid, "start": utc_iso(local(2026, 11, 16, h)),
                "end": utc_iso(local(2026, 11, 16, h + 1)), "all_day": 0,
                "reminder": reminder, "cancelled": 0, "response": "organizer", **kw}

    rows = [ev("soon", 9), ev("later", 11), ev("none", 9, reminder=None),
            ev("declined", 9, response="declined"), ev("running", 8), ev("done", 7)]
    assert [r["id"] for r in due_reminders(rows, now, {})] == ["running", "soon"]
    soon = rows[0]
    later = utc_iso(now + dt.timedelta(minutes=5))
    assert [r["id"] for r in due_reminders(rows, now, {("soon", soon["start"]):
                                                       (False, later)})] == ["running"]
    assert [r["id"] for r in due_reminders(rows, now, {("soon", soon["start"]):
                                                       (True, None)})] == ["running"]
    st = Store(tmp_path / "m.db")
    st.set_reminder_state("soon", soon["start"], snoozed_until=later)
    assert st.reminder_states() == {("soon", soon["start"]): (False, later)}


def test_series_and_reminder_requests():
    class S:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, headers=None, json=None, **kw):
            self.calls.append((method, url.split("v1.0")[1], json, params))
            return FakeResponse(200, {"id": "M", "recurrence": {}})

    s = S()
    g = GraphClient(lambda: "t", session=s)
    g.get_event("M")
    g.dismiss_reminder("E")
    g.snooze_reminder("E", "2026-11-16T15:05:00Z")
    assert "recurrence" in s.calls[0][3]["$select"]
    assert [c[:2] for c in s.calls[1:]] == [("POST", "/me/events/E/dismissReminder"),
                                            ("POST", "/me/events/E/snoozeReminder")]
    assert s.calls[2][2] == {"newReminderTime": {"dateTime": "2026-11-16T15:05:00",
                                                 "timeZone": "UTC"}}
