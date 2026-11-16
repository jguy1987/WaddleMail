"""Tasks: Microsoft To Do's lists and tasks (no Qt imports; unit-testable).

The same tasks as Outlook and the To Do app. Each list is kept in step with a delta query; the
Tasks page groups what's open by when it's due (Overdue, Today, Tomorrow, This week, ...), as
Outlook 2010's To-Do List did. Due and start dates are days; a reminder is a moment (UTC).
Mail flagged in Outlook shows up in To Do's "Flagged email" list.

A repeating task has a recurrence (the same patternedRecurrence as events) starting on its
due date. Completing it makes To Do create the next one, due on the next date of the pattern;
the completed one stays as it was.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field

from .calendars import parse_utc, utc_iso
from .graph import DeltaResetError, GraphClient
from .store import Store

log = logging.getLogger(__name__)

STATUS = [("notStarted", "Not started"), ("inProgress", "In progress"),
          ("waitingOnOthers", "Waiting on someone else"), ("deferred", "Deferred"),
          ("completed", "Completed")]
STATUS_LABEL = dict(STATUS)
IMPORTANCE = [("low", "Low"), ("normal", "Normal"), ("high", "High")]

# The To-Do List's groups, in order.
GROUPS = ["Overdue", "Today", "Tomorrow", "This week", "Next week", "Later", "No date",
          "Completed"]

# Outlook's quick follow-up dates.
QUICK = [("today", "&Today"), ("tomorrow", "To&morrow"), ("this_week", "This &week"),
         ("next_week", "&Next week"), ("none", "No &date")]


# -- dates ---------------------------------------------------------------------------
def due_date(value: dict | None) -> dt.date | None:
    """A task's due (or start) date. To Do keeps it as midnight of the day: in the time zone
    it was set in (the day as written), or, from some apps, in UTC as the moment your local
    midnight was (converted back to your day). A plain UTC midnight is the day itself."""
    if not value or not value.get("dateTime"):
        return None
    t = dt.datetime.fromisoformat(value["dateTime"][:19])
    if (value.get("timeZone") or "UTC").upper() in ("UTC", "ETC/UTC") and t.time() != dt.time():
        return t.replace(tzinfo=dt.timezone.utc).astimezone().date()
    return t.date()


def moment(value: dict | None) -> str | None:
    """A dateTimeTimeZone that Graph sends in UTC, as a UTC ISO string."""
    if not value or not value.get("dateTime"):
        return None
    return utc_iso(parse_utc(value["dateTime"]))


def week_end(day: dt.date) -> dt.date:
    """The Sunday that ends `day`'s week."""
    return day + dt.timedelta(days=6 - day.weekday())


def due_group(due: dt.date | None, today: dt.date, completed: bool = False) -> str:
    if completed:
        return "Completed"
    if due is None:
        return "No date"
    if due < today:
        return "Overdue"
    if due == today:
        return "Today"
    if due == today + dt.timedelta(days=1):
        return "Tomorrow"
    if due <= week_end(today):
        return "This week"
    if due <= week_end(today) + dt.timedelta(days=7):
        return "Next week"
    return "Later"


def quick_due(choice: str, today: dt.date) -> dt.date | None:
    """Outlook's follow-up flags: Today, Tomorrow, This week (its last working day, or today
    at the weekend), Next week (Friday), No date."""
    if choice == "today":
        return today
    if choice == "tomorrow":
        return today + dt.timedelta(days=1)
    friday = today + dt.timedelta(days=4 - today.weekday())
    if choice == "this_week":
        return max(friday, today)
    if choice == "next_week":
        return friday + dt.timedelta(days=7)
    return None


def due_text(due: dt.date | None, today: dt.date) -> str:
    """"Due today", "Overdue by 3 days", "Due Fri 20 Nov"."""
    if due is None:
        return "No due date"
    days = (due - today).days
    if days < 0:
        return f"Overdue by {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    return f"Due {due:%a} {due.day} {due:%b}"


# -- rows ----------------------------------------------------------------------------
def list_row(item: dict, account_id: str) -> dict:
    return {"id": item["id"], "account_id": account_id,
            "name": item.get("displayName") or "Tasks",
            "well_known": item.get("wellknownListName") or "none",
            "shared": int(bool(item.get("isShared")))}


def task_row(item: dict, account_id: str, list_id: str) -> dict:
    due, start = due_date(item.get("dueDateTime")), due_date(item.get("startDateTime"))
    body = item.get("body") or {}
    return {
        "id": item["id"], "account_id": account_id, "list_id": list_id,
        "title": item.get("title") or "", "status": item.get("status") or "notStarted",
        "importance": item.get("importance") or "normal",
        "due": due.isoformat() if due else None, "start": start.isoformat() if start else None,
        "reminder": moment(item.get("reminderDateTime")) if item.get("isReminderOn") else None,
        "completed": moment(item.get("completedDateTime")),
        "body": body.get("content") or "", "body_type": (body.get("contentType") or "text"),
        "categories": json.dumps(item.get("categories") or []),
        "created": item.get("createdDateTime"), "modified": item.get("lastModifiedDateTime"),
        "recurrence": json.dumps(item["recurrence"]) if item.get("recurrence") else None,
    }


def link_rows(items: list[dict]) -> list[list]:
    """linkedResources as [[name, web url, external id, application], ...]."""
    return [[r.get("displayName") or "", r.get("webUrl") or "", r.get("externalId") or "",
             r.get("applicationName") or ""] for r in items]


@dataclass
class TaskDraft:
    """What the task editor produces; fields() turns it into a Graph todoTask."""
    title: str
    due: dt.date | None = None
    start: dt.date | None = None
    reminder: dt.datetime | None = None   # aware; None: no reminder
    importance: str = "normal"
    status: str = "notStarted"
    categories: tuple = ()
    body: str = ""
    links: list = field(default_factory=list)  # [(name, web url, external id)]: new tasks
    recurrence: dict | None = None        # make_recurrence(...) from the due date; needs one
    tz: str = "UTC"                       # your time zone (Windows name), for the dates

    def _day(self, day: dt.date | None):
        return {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": self.tz} if day else None

    def _recurrence(self) -> dict | None:
        """The pattern, starting on the due date (To Do wants one, and its time zone)."""
        if not self.recurrence or self.due is None:
            return None
        rec = json.loads(json.dumps(self.recurrence))  # (a copy)
        rec["range"]["startDate"] = self.due.isoformat()
        rec["range"]["recurrenceTimeZone"] = self.tz
        return rec

    def fields(self, new: bool = False) -> dict:
        out = {"title": self.title, "importance": self.importance, "status": self.status,
               "categories": list(self.categories),
               "body": {"contentType": "text", "content": self.body},
               "dueDateTime": self._day(self.due), "startDateTime": self._day(self.start),
               "isReminderOn": self.reminder is not None,
               "recurrence": self._recurrence()}
        if self.reminder is not None:
            out["reminderDateTime"] = {"dateTime": utc_iso(self.reminder).rstrip("Z"),
                                       "timeZone": "UTC"}
        if new:
            out = {k: v for k, v in out.items() if v is not None}
            if self.links:
                out["linkedResources"] = [
                    {"displayName": name, "webUrl": url, "externalId": ext,
                     "applicationName": "WaddleMail"} for name, url, ext in self.links]
        return out


def due_reminders(rows, now: dt.datetime) -> list:
    """Open tasks whose reminder has gone off, earliest first."""
    return sorted((r for r in rows if r["reminder"] and r["status"] != "completed"
                   and parse_utc(r["reminder"]) <= now), key=lambda r: r["reminder"])


# -- sync ----------------------------------------------------------------------------
class TaskSync:
    def __init__(self, store: Store, graph: GraphClient, account_id: str) -> None:
        self.store, self.graph, self.account_id = store, graph, account_id

    def sync(self) -> int:
        """The lists, then each list's changes since last time. Returns tasks changed."""
        lists = [list_row(i, self.account_id) for i in self.graph.todo_lists()]
        self.store.replace_task_lists(self.account_id, lists)
        changed = 0
        for lst in lists:
            key = f"todo:{lst['id']}"
            link = self.store.get_state(key)
            try:
                items, link = self.graph.tasks_delta(lst["id"], link)
            except DeltaResetError:  # too old: start again
                self.store.delete_state(key)
                self.store.clear_task_list(lst["id"])
                items, link = self.graph.tasks_delta(lst["id"], None)
            gone = [i["id"] for i in items if "@removed" in i]
            rows = [task_row(i, self.account_id, lst["id"]) for i in items if "@removed" not in i]
            self.store.apply_task_changes(rows, gone)
            if link:
                self.store.set_state(key, link)
            changed += len(rows) + len(gone)
        return changed

    def save(self, list_id: str, draft: TaskDraft, task_id: str | None = None) -> dict:
        """Create or change a task; returns its row (as stored)."""
        item = self.graph.save_task(list_id, draft.fields(new=task_id is None), task_id)
        row = task_row(item, self.account_id, list_id)
        if task_id is None and draft.links:
            row["links"] = json.dumps([[n, u, e, "WaddleMail"] for n, u, e in draft.links])
        self.store.upsert_task(row)
        return row

    def move(self, task_id: str, list_id: str, draft: TaskDraft) -> dict:
        """To Do can't move a task between lists: make it again there, then delete it."""
        old = self.store.task(task_id)
        links = json.loads(old["links"]) if old is not None and old["links"] else []
        draft.links = draft.links or [(n, u, e) for n, u, e, *_ in links]
        row = self.save(list_id, draft)
        self.delete(old["list_id"], task_id)
        return row

    def update(self, list_id: str, task_id: str, changes: dict) -> dict:
        """A few fields (completed, due, reminder off/snoozed)."""
        item = self.graph.save_task(list_id, changes, task_id)
        row = task_row(item, self.account_id, list_id)
        self.store.upsert_task(row)
        return row

    def delete(self, list_id: str, task_id: str) -> None:
        self.graph.delete_task(list_id, task_id)
        self.store.delete_task(task_id)

    def links(self, list_id: str, task_id: str) -> list[list]:
        """The task's links (to the email it came from), fetched and kept."""
        rows = link_rows(self.graph.task_links(list_id, task_id))
        self.store.set_task_links(task_id, rows)
        return rows
