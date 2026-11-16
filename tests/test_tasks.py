"""Tasks: To Do's dates, the To-Do List's groups, drafts, and syncing lists and tasks."""
from __future__ import annotations

import datetime as dt
import json

from waddlemail.graph import DeltaResetError
from waddlemail.store import Store
from waddlemail.tasks import (
    TaskDraft, TaskSync, due_date, due_group, due_reminders, due_text, quick_due, task_row,
)

WED = dt.date(2026, 11, 18)  # a Wednesday


def test_due_dates_are_days():
    assert due_date({"dateTime": "2026-11-20T00:00:00.0000000", "timeZone": "UTC"}) == \
        dt.date(2026, 11, 20)  # UTC midnight: that day, not the evening before
    assert due_date({"dateTime": "2026-11-20T00:00:00", "timeZone": "Eastern Standard Time"}) \
        == dt.date(2026, 11, 20)
    local_midnight = dt.datetime(2026, 11, 20).astimezone()  # stored as its UTC moment
    utc = local_midnight.astimezone(dt.timezone.utc)
    if utc.time() != dt.time():
        assert due_date({"dateTime": utc.strftime("%Y-%m-%dT%H:%M:%S"),
                         "timeZone": "UTC"}) == dt.date(2026, 11, 20)
    assert due_date(None) is None and due_date({"dateTime": None}) is None


def test_groups_and_quick_dates():
    groups = [due_group(WED + dt.timedelta(days=n), WED) for n in (-1, 0, 1, 4, 5, 11, 12)]
    assert groups == ["Overdue", "Today", "Tomorrow", "This week", "Next week", "Next week",
                      "Later"]
    assert due_group(None, WED) == "No date" and due_group(WED, WED, True) == "Completed"
    assert [quick_due(c, WED) for c in ("today", "tomorrow", "this_week", "next_week", "none")] \
        == [WED, WED + dt.timedelta(days=1), dt.date(2026, 11, 20), dt.date(2026, 11, 27), None]
    saturday = dt.date(2026, 11, 21)
    assert quick_due("this_week", saturday) == saturday  # (the weekend: today)
    assert due_text(WED - dt.timedelta(days=2), WED) == "Overdue by 2 days"
    assert due_text(dt.date(2026, 11, 20), WED) == "Due Fri 20 Nov"


def test_drafts_and_rows():
    remind = dt.datetime(2026, 11, 20, 8, tzinfo=dt.timezone.utc)
    d = TaskDraft("Send the report", due=dt.date(2026, 11, 20), reminder=remind,
                  importance="high", categories=("Red",), body="Q4",
                  links=[("Q4 numbers", "https://outlook/x", "m1")], tz="W. Europe Standard Time")
    new = d.fields(new=True)
    assert new["dueDateTime"] == {"dateTime": "2026-11-20T00:00:00",
                                  "timeZone": "W. Europe Standard Time"}
    assert "startDateTime" not in new and new["isReminderOn"]
    assert new["reminderDateTime"] == {"dateTime": "2026-11-20T08:00:00", "timeZone": "UTC"}
    assert new["linkedResources"][0]["externalId"] == "m1"
    changed = TaskDraft("x").fields()
    assert changed["dueDateTime"] is None and changed["isReminderOn"] is False  # (cleared)
    row = task_row({"id": "t1", "title": "Send", "status": "notStarted",
                    "dueDateTime": new["dueDateTime"], "isReminderOn": True,
                    "reminderDateTime": {"dateTime": "2026-11-20T08:00:00.0000000",
                                         "timeZone": "UTC"}}, "a", "L")
    assert (row["due"], row["reminder"]) == ("2026-11-20", "2026-11-20T08:00:00Z")
    rows = [dict(row), dict(row, id="t2", status="completed"),
            dict(row, id="t3", reminder="2026-11-21T08:00:00Z")]
    assert [r["id"] for r in due_reminders(rows, remind + dt.timedelta(minutes=1))] == ["t1"]


class ToDo:
    def __init__(self):
        self.lists = [{"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"},
                      {"id": "L2", "displayName": "Flagged email",
                       "wellknownListName": "flaggedEmails"}]
        self.items = {"L1": [{"id": "t1", "title": "Pay rent", "status": "notStarted",
                              "dueDateTime": {"dateTime": "2026-11-01T00:00:00",
                                              "timeZone": "UTC"}}],
                      "L2": [{"id": "t2", "title": "Re: budget", "status": "notStarted"}]}
        self.links_asked = 0
        self.reset = False

    def todo_lists(self):
        return self.lists

    def tasks_delta(self, list_id, link):
        if link and self.reset:
            self.reset = False
            raise DeltaResetError("expired", 410)
        items = self.items[list_id] if link is None else [
            i for i in self.items[list_id] if i.get("changed") or "@removed" in i]
        return items, f"delta-{list_id}"

    def save_task(self, list_id, fields, task_id=None):
        return {"id": task_id or "new", **{k: v for k, v in fields.items()
                                           if k != "linkedResources"}}

    def delete_task(self, list_id, task_id):
        pass

    def task_links(self, list_id, task_id):
        self.links_asked += 1
        return [{"displayName": "Re: budget", "webUrl": "https://outlook/m",
                 "externalId": "m9", "applicationName": "Outlook"}]


def test_sync_lists_and_tasks(tmp_path):
    st, g = Store(tmp_path / "m.db"), ToDo()
    s = TaskSync(st, g, "a")
    assert s.sync() == 2
    assert [r["well_known"] for r in st.task_lists()] == ["defaultList", "flaggedEmails"]
    assert st.task_counts("2026-11-18") == {"L1": (1, 1), "L2": (1, 0)}
    g.items["L1"] = [{"id": "t1", "@removed": {"reason": "deleted"}}]
    g.items["L2"][0].update(title="Re: budget (call Ana)", changed=True)
    s.sync()
    assert st.task("t1") is None and st.task("t2")["title"] == "Re: budget (call Ana)"
    assert json.loads(json.dumps(s.links("L2", "t2")))[0][2] == "m9"
    s.sync()  # links stay through later changes
    assert json.loads(st.task("t2")["links"])[0][2] == "m9"
    g.reset = True
    s.sync()  # (an expired delta: everything again)
    assert st.task("t2") is not None
    g.lists = g.lists[:1]
    s.sync()  # a list deleted elsewhere: its tasks go
    assert st.task("t2") is None and len(st.task_lists()) == 1
    made = s.save("L1", TaskDraft("New", links=[("mail", "https://x", "m1")]))
    assert json.loads(st.task(made["id"])["links"])[0][2] == "m1"


def test_repeating_tasks_repeat_from_the_due_date():
    from waddlemail.calendars import make_recurrence
    weekly = make_recurrence({"type": "weekly", "interval": 1, "daysOfWeek": ["friday"]},
                             dt.date(2026, 1, 1))
    d = TaskDraft("Timesheet", due=dt.date(2026, 11, 20), recurrence=weekly,
                  tz="Eastern Standard Time")
    rec = d.fields(new=True)["recurrence"]
    assert rec["pattern"]["daysOfWeek"] == ["friday"]
    assert rec["range"]["startDate"] == "2026-11-20"
    assert rec["range"]["recurrenceTimeZone"] == "Eastern Standard Time"
    assert weekly["range"]["startDate"] == "2026-01-01"  # (the original isn't changed)
    assert "recurrence" not in TaskDraft("x", recurrence=weekly).fields(new=True)  # no due
    assert TaskDraft("x", due=dt.date(2026, 11, 20)).fields()["recurrence"] is None  # stops
