"""Thin Microsoft Graph client: paging, throttling/retry, delta, message bodies."""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Callable, Iterator
from urllib.parse import quote

import requests

from . import config
from .auth import AuthError
from .inline import MAX_INLINE_IMAGE, cid_key, image_type

log = logging.getLogger(__name__)

# Connection reporting: called (from whichever thread made the request) with "online" when
# the server answered (whatever it said), "offline" when it couldn't be reached, or "signin"
# when no token could be had; plus a detail text. The UI's status indicator listens.
ONLINE, OFFLINE, SIGNIN = "online", "offline", "signin"
status_hook: Callable[[str, str], None] | None = None


def _report(state: str, detail: str = "") -> None:
    if status_hook is not None:
        try:
            status_hook(state, detail)
        except Exception:  # reporting must never break a request
            log.debug("status hook failed", exc_info=True)

FOLDER_FIELDS = "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount"
# Default folders, in the order the folder pane shows them (other folders follow, A-Z).
WELL_KNOWN = ("inbox", "drafts", "sentitems", "deleteditems", "junkemail", "archive", "outbox")
# Exchange's own bookkeeping folders. They are recognised (so they can't be renamed, moved or
# deleted) but listed among the other folders, A-Z.
SYSTEM = ("syncissues", "conflicts", "localfailures", "serverfailures")
# Folders that can't contain folders (Exchange refuses, and so does Outlook).
NO_SUBFOLDERS = frozenset({"outbox", *SYSTEM})
MESSAGE_FIELDS = (
    "id,parentFolderId,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "isRead,hasAttachments,importance,flag,bodyPreview"
)
CONTACT_FIELDS = (
    "id,displayName,givenName,surname,emailAddresses,businessPhones,mobilePhone,homePhones,"
    "companyName,jobTitle,department,officeLocation,personalNotes,parentFolderId,"
    "businessAddress,homeAddress,birthday,businessHomePage,nickName"
)
DIRECTORY_FIELDS = (
    "id,displayName,givenName,surname,mail,userPrincipalName,jobTitle,department,"
    "officeLocation,businessPhones,mobilePhone,companyName"
)
EVENT_FIELDS = (
    "id,subject,start,end,isAllDay,showAs,categories,location,organizer,attendees,"
    "isOnlineMeeting,onlineMeeting,onlineMeetingProvider,bodyPreview,isCancelled,"
    "responseStatus,seriesMasterId,type,isOrganizer,sensitivity,reminderMinutesBeforeStart,"
    "isReminderOn"
)
CALENDAR_FIELDS = ("id,name,hexColor,color,isDefaultCalendar,canEdit,"
                   "allowedOnlineMeetingProviders,owner")
# MAPI properties Graph has no first-class field for: PidTagLastVerbExecuted (102 replied,
# 103 replied to all, 104 forwarded) and PidTagMessageSize (bytes). The delta query doesn't
# return extended properties (it accepts $expand but sends nothing back), and the $filter in
# $expand=singleValueExtendedProperties takes only "id eq" (one property per request). So:
# sizes come from message_sizes(), the reply state from messages_with_verb() (a one-time
# back-fill per folder) and message_verbs() (for messages a sync reports as changed).
PROP_LAST_VERB = "Integer 0x1081"
PROP_SIZE = "Integer 0x0E08"
VERBS = (102, 103, 104)
# Delta cursors remember the query they were made with: when it changes, they are made again.
DELTA_QUERY_VERSION = "2"  # 2: reply state with a single "id eq" filter


def _network_problem(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "The server didn't answer in time."
    if isinstance(exc, requests.ConnectionError):
        return "Can't reach the server (no network connection?)."
    return f"Network error: {exc.__class__.__name__}"


class GraphError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DeltaResetError(GraphError):
    """The stored delta link is no longer valid (HTTP 410); do a full resync."""


def _prefer(*values: str) -> dict[str, str]:
    # ImmutableId keeps a message's id stable when it moves between folders,
    # which keeps cached bodies valid.
    return {"Prefer": ", ".join(values + ('IdType="ImmutableId"',))}


class GraphClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        base: str = config.GRAPH_BASE,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        user: str | None = None,
    ) -> None:
        """`user` is a shared/delegated mailbox address; None means the signed-in user's own."""
        self._token = token_provider
        self._base = base.rstrip("/")
        self._root = f"/users/{quote(user, safe='@')}" if user else "/me"

        self._s = session or requests.Session()
        self._sleep = sleep

    # -- low level -----------------------------------------------------
    def _url(self, url: str) -> str:
        return url if url.startswith("http") else self._base + url

    def _request(self, url: str, params: dict | None = None, headers: dict | None = None,
                 retries: int = 5, method: str = "GET", json: dict | None = None,
                 data: bytes | None = None, raw: bool = False):
        """JSON in and out; or `data` bytes in and, with `raw`, the response bytes out."""
        full = self._url(url)
        refreshed = False
        for attempt in range(retries):
            try:
                hdrs = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
                r = self._s.request(method, full, params=params, headers=hdrs, json=json,
                                    data=data, timeout=30)
            except AuthError as exc:
                _report(SIGNIN, str(exc))
                raise
            except requests.RequestException as exc:  # DNS, refused, timeout, TLS, ...
                _report(OFFLINE, _network_problem(exc))
                raise
            _report(ONLINE)
            if r.status_code in (429, 503, 504):
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                log.info("Graph %s; retrying in %.1fs", r.status_code, wait)
                self._sleep(min(wait, 60))
                continue
            if r.status_code == 401 and not refreshed:
                refreshed = True  # token provider re-validates on next call
                continue
            if r.status_code == 410:
                raise DeltaResetError("sync state expired", 410)
            if not r.ok:
                raise GraphError(f"Graph {r.status_code}: {r.text[:300]}", r.status_code)
            if raw:
                return r.content
            return r.json() if r.text else {}
        raise GraphError("Graph request failed after retries")

    def _paged(self, url: str, params: dict | None = None,
               headers: dict | None = None) -> Iterator[dict]:
        while url:
            data = self._request(url, params=params, headers=headers)
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query

    # -- folders -------------------------------------------------------
    def inbox_id(self) -> str:
        return self._request(f"{self._root}/mailFolders/inbox", params={"$select": "id"})["id"]

    def well_known_folders(self) -> dict[str, dict]:
        """Map well-known name -> folder, in one $batch request. Folders that don't exist
        (no Archive yet) or that we may not open (a shared folder set) are left out."""
        reqs = [{"id": name, "method": "GET",
                 "url": f"{self._root}/mailFolders/{name}?$select={FOLDER_FIELDS}"}
                for name in WELL_KNOWN + SYSTEM]
        data = self._request("/$batch", method="POST", json={"requests": reqs})
        return {r["id"]: r["body"] for r in data.get("responses", [])
                if r.get("status") == 200 and isinstance(r.get("body"), dict)}

    def list_folders(self) -> list[dict]:
        """Every folder we can see, with "wellKnownName" added to the default ones.

        With full access to the mailbox this walks the whole tree. When only some folders are
        shared with us (someone shares just their Inbox, say), listing the root is refused, so
        we start from whichever default folders we may open and walk below those instead.
        """
        known = self.well_known_folders()
        names = {f["id"]: name for name, f in known.items()}
        out: list[dict] = []

        def walk(url: str) -> None:
            for f in self._paged(url, params={"$top": 100, "$select": FOLDER_FIELDS}):
                out.append(f)
                children(f)

        def children(f: dict) -> None:
            if not f.get("childFolderCount"):
                return
            try:
                walk(f"{self._root}/mailFolders/{quote(f['id'], safe='')}/childFolders")
            except GraphError as exc:  # a subfolder that wasn't shared with us
                if exc.status not in (403, 404):
                    raise

        try:
            walk(f"{self._root}/mailFolders")
        except GraphError as exc:
            if exc.status not in (403, 404) or not known:
                raise
            log.info("cannot list %s; using its shared folders only", self._root)
            out.clear()
            for f in known.values():
                out.append(f)
                children(f)
        for f in out:
            if f["id"] in names:
                f["wellKnownName"] = names[f["id"]]
        return out

    def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        """A new folder under `parent_id`, or at the top of the mailbox."""
        url = f"{self._root}/mailFolders"
        if parent_id:
            url += f"/{quote(parent_id, safe='')}/childFolders"
        return self._request(url, method="POST", json={"displayName": name},
                             headers=_prefer())

    def rename_folder(self, folder_id: str, name: str) -> dict:
        return self._request(f"{self._root}/mailFolders/{quote(folder_id, safe='')}",
                             method="PATCH", json={"displayName": name}, headers=_prefer())

    def move_folder(self, folder_id: str, destination_id: str) -> dict:
        """Move under another folder; `destination_id` may be a well-known name."""
        return self._request(f"{self._root}/mailFolders/{quote(folder_id, safe='')}/move",
                             method="POST", json={"destinationId": destination_id},
                             headers=_prefer())

    def delete_folder(self, folder_id: str) -> None:
        """Remove a folder and everything in it (recoverable only by an administrator).
        To delete the way Outlook does, move it to Deleted Items instead."""
        self._request(f"{self._root}/mailFolders/{quote(folder_id, safe='')}", method="DELETE")

    # -- calendar ------------------------------------------------------
    # Times are read and written in UTC (Prefer outlook.timezone); WaddleMail converts to local.
    def calendars(self) -> list[dict]:
        return list(self._paged(f"{self._root}/calendars",
                                params={"$top": 100, "$select": CALENDAR_FIELDS}))

    def calendar_view(self, calendar_id: str, start: str, end: str) -> list[dict]:
        """Events (recurring ones expanded into occurrences) overlapping [start, end),
        given as UTC ISO times."""
        return list(self._paged(
            f"{self._root}/calendars/{quote(calendar_id, safe='')}/calendarView",
            params={"startDateTime": start, "endDateTime": end, "$top": 250,
                    "$select": EVENT_FIELDS},
            headers=_prefer('outlook.timezone="UTC"')))

    def get_event(self, event_id: str) -> dict:
        """One event with its recurrence (for a series' master event)."""
        return self._request(f"{self._root}/events/{quote(event_id, safe='')}",
                             params={"$select": EVENT_FIELDS + ",recurrence"},
                             headers=_prefer('outlook.timezone="UTC"'))

    def dismiss_reminder(self, event_id: str) -> None:
        self._request(f"{self._root}/events/{quote(event_id, safe='')}/dismissReminder",
                      method="POST")

    def snooze_reminder(self, event_id: str, until_utc: str) -> None:
        self._request(f"{self._root}/events/{quote(event_id, safe='')}/snoozeReminder",
                      method="POST", json={"newReminderTime": {
                          "dateTime": until_utc.rstrip("Z"), "timeZone": "UTC"}})

    def event_body(self, event_id: str) -> tuple[str, str]:
        data = self._request(f"{self._root}/events/{quote(event_id, safe='')}",
                             params={"$select": "body"}, headers=_prefer())
        body = data.get("body") or {}
        return (body.get("contentType") or "text").lower(), body.get("content") or ""

    def save_event(self, fields: dict, event_id: str | None = None,
                   calendar_id: str | None = None) -> dict:
        """Create (in `calendar_id`, else the default calendar) or update an event. With
        attendees, Exchange sends the invitations / updates."""
        hdrs = _prefer('outlook.timezone="UTC"')
        if event_id:
            return self._request(f"{self._root}/events/{quote(event_id, safe='')}",
                                 method="PATCH", json=fields, headers=hdrs)
        url = (f"{self._root}/calendars/{quote(calendar_id, safe='')}/events" if calendar_id
               else f"{self._root}/events")
        return self._request(url, method="POST", json=fields, headers=hdrs)

    def delete_event(self, event_id: str) -> None:
        """Delete; for a meeting you organised this also cancels it for the attendees."""
        self._request(f"{self._root}/events/{quote(event_id, safe='')}", method="DELETE")

    def respond_event(self, event_id: str, response: str, comment: str = "") -> None:
        """`response`: accept / tentativelyAccept / decline (the organizer is told)."""
        self._request(f"{self._root}/events/{quote(event_id, safe='')}/{response}",
                      method="POST", json={"comment": comment, "sendResponse": True})

    def schedule(self, addresses: list[str], start: str, end: str,
                 interval: int = 30) -> list[dict]:
        """Free/busy for people (UTC ISO times): one entry per address with scheduleItems
        (start, end, status) and availabilityView (a digit per `interval` minutes)."""
        data = self._request(
            f"{self._root}/calendar/getSchedule", method="POST",
            json={"schedules": addresses,
                  "startTime": {"dateTime": start.rstrip("Z"), "timeZone": "UTC"},
                  "endTime": {"dateTime": end.rstrip("Z"), "timeZone": "UTC"},
                  "availabilityViewInterval": interval},
            headers=_prefer('outlook.timezone="UTC"'))
        return data.get("value") or []

    def categories(self) -> list[dict]:
        """Your Outlook categories: displayName and color (preset0..preset24 or none)."""
        return self._request(f"{self._root}/outlook/masterCategories").get("value") or []

    # -- contacts ------------------------------------------------------
    def contact_folders(self) -> list[dict]:
        """Every contact folder below the default Contacts folder (which isn't listed)."""
        out: list[dict] = []

        def walk(url: str) -> None:
            for f in self._paged(url, params={"$top": 100, "$select": "id,displayName"}):
                out.append(f)
                walk(f"{self._root}/contactFolders/{quote(f['id'], safe='')}/childFolders")

        walk(f"{self._root}/contactFolders")
        return out

    def contacts(self, folder_id: str | None = None) -> list[dict]:
        """All contacts in a folder (None: the default Contacts folder)."""
        url = (f"{self._root}/contactFolders/{quote(folder_id, safe='')}/contacts" if folder_id
               else f"{self._root}/contacts")
        return list(self._paged(url, params={"$top": 500, "$select": CONTACT_FIELDS},
                                headers=_prefer()))

    def save_contact(self, fields: dict, contact_id: str | None = None,
                     folder_id: str | None = None) -> dict:
        """Create (no `contact_id`) or update a personal contact; returns it."""
        if contact_id:
            return self._request(f"{self._root}/contacts/{quote(contact_id, safe='')}",
                                 method="PATCH", json=fields, headers=_prefer())
        url = (f"{self._root}/contactFolders/{quote(folder_id, safe='')}/contacts" if folder_id
               else f"{self._root}/contacts")
        return self._request(url, method="POST", json=fields, headers=_prefer())

    def delete_contact(self, contact_id: str) -> None:
        self._request(f"{self._root}/contacts/{quote(contact_id, safe='')}", method="DELETE")

    def directory_users(self, limit: int = 50_000) -> list[dict]:
        """People in the organization's directory (work/school accounts only)."""
        out: list[dict] = []
        for u in self._paged("/users", params={"$top": 999, "$select": DIRECTORY_FIELDS}):
            out.append(u)
            if len(out) >= limit:
                break
        return out

    # -- photos --------------------------------------------------------
    def _photo_url(self, contact_id: str, folder_id: str | None = None) -> str:
        if contact_id.startswith("dir:"):  # a directory person
            return f"/users/{quote(contact_id[4:], safe='')}/photo/$value"
        if folder_id:
            return (f"{self._root}/contactFolders/{quote(folder_id, safe='')}/contacts/"
                    f"{quote(contact_id, safe='')}/photo/$value")
        return f"{self._root}/contacts/{quote(contact_id, safe='')}/photo/$value"

    def photo(self, contact_id: str, folder_id: str | None = None) -> bytes | None:
        """The photo of a contact or directory person (JPEG/PNG bytes), or None."""
        try:
            return self._request(self._photo_url(contact_id, folder_id), raw=True) or None
        except GraphError as exc:
            if exc.status in (404, 400):  # no photo
                return None
            raise

    def set_contact_photo(self, contact_id: str, image: bytes,
                          folder_id: str | None = None) -> None:
        self._request(self._photo_url(contact_id, folder_id), method="PUT", data=image,
                      headers={"Content-Type": "image/jpeg"})

    # -- contact groups (beta) -------------------------------------------
    def _groups_url(self, group_id: str | None = None) -> str:
        beta = self._base.rsplit("/", 1)[0] + "/beta"
        url = f"{beta}{self._root}/distributionLists"
        return url + f"/{quote(group_id, safe='')}" if group_id else url

    def create_group(self, name: str, notes: str = "") -> dict:
        return self._request(self._groups_url(), method="POST",
                             json={"displayName": name, "notes": notes}, headers=_prefer())

    def update_group(self, group_id: str, name: str, notes: str) -> dict:
        return self._request(self._groups_url(group_id), method="PATCH",
                             json={"displayName": name, "notes": notes}, headers=_prefer())

    def delete_group(self, group_id: str) -> None:
        self._request(self._groups_url(group_id), method="DELETE")

    def add_group_members(self, group_id: str, members: list[dict]) -> None:
        """`members`: {"displayName", "key" (address), "type"}. If the server won't take a
        member as a contact or mailbox, add everyone as plain addresses (oneOff) instead."""
        if not members:
            return
        url = self._groups_url(group_id) + "/addMembers"
        try:
            self._request(url, method="POST", json={"members": members})
        except GraphError as exc:
            if exc.status != 400 or all(m["type"] == "oneOff" for m in members):
                raise
            log.info("addMembers refused typed members (%s); adding as addresses", exc)
            plain = [{**m, "type": "oneOff"} for m in members]
            plain = [{k: v for k, v in m.items() if k != "memberId"} for m in plain]
            self._request(url, method="POST", json={"members": plain})

    def remove_group_members(self, group_id: str, members: list[dict]) -> None:
        """`members`: {"type", "key" and/or "memberId"} as the group listed them."""
        if members:
            self._request(self._groups_url(group_id) + "/deleteMembers", method="POST",
                          json={"members": members})

    def contact_groups(self) -> list[dict]:
        """Personal contact groups with their members. Microsoft Graph only offers these in
        its beta API (distributionLists, since 2026), so this may change or be missing."""
        groups = list(self._paged(self._groups_url(), params={"$top": 100}, headers=_prefer()))
        for g in groups:
            url = self._groups_url(g["id"])
            try:  # members with their contacts' addresses, if the server can nest that
                full = self._request(url, params={"$expand": "members($expand=contact)"},
                                     headers=_prefer())
            except GraphError as exc:
                if exc.status != 400:
                    raise
                full = self._request(url, params={"$expand": "members"}, headers=_prefer())
            g["members"] = full.get("members") or []
        return groups

    # -- messages ------------------------------------------------------
    def messages_delta(self, folder_id: str, delta_link: str | None = None,
                       since: str | None = None) -> Iterator[tuple[list[dict], str | None]]:
        """Yield (items, delta_link). delta_link is set only on the final page."""
        if delta_link:
            url, params = delta_link, None
        else:
            url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages/delta"
            params = {"$select": MESSAGE_FIELDS}
            if since:
                params["$filter"] = f"receivedDateTime ge {since}"
        headers = _prefer("odata.maxpageSize=100")
        while True:
            data = self._request(url, params=params, headers=headers)
            nxt = data.get("@odata.nextLink")
            yield data.get("value", []), data.get("@odata.deltaLink")
            if not nxt:
                return
            url, params = nxt, None

    def messages_with_verb(self, folder_id: str, verb: int) -> list[str]:
        """Ids of the folder's messages last replied (102) / replied to all (103) /
        forwarded (104), by Graph's documented filter on an extended property's value."""
        url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages"
        params = {"$select": "id", "$top": 500,
                  "$filter": ("singleValueExtendedProperties/any(ep: ep/id eq "
                              f"'{PROP_LAST_VERB}' and cast(ep/value, Edm.Int32) eq {verb})")}
        return [m["id"] for m in self._paged(url, params=params, headers=_prefer())]

    def message_verbs(self, message_ids: list[str]) -> dict[str, int | None]:
        """The reply state of particular messages ({id: 102/103/104 or None}), 20 per $batch
        request, each a documented single-message $expand of the property."""
        expand = quote(f"singleValueExtendedProperties($filter=id eq '{PROP_LAST_VERB}')",
                       safe="()$='")
        out: dict[str, int | None] = {}
        for i in range(0, len(message_ids), 20):
            chunk = message_ids[i:i + 20]
            reqs = [{"id": str(n), "method": "GET", "headers": _prefer(),  # (our ids are
                     "url": f"{self._root}/messages/{quote(mid, safe='')}?$select=id&"  # immutable)
                            f"$expand={expand}"}
                    for n, mid in enumerate(chunk)]
            data = self._request("/$batch", method="POST", json={"requests": reqs},
                                 headers=_prefer())
            for r in data.get("responses", []):
                if r.get("status") != 200 or not isinstance(r.get("body"), dict):
                    continue  # gone, or not ours to read: leave it as it is
                mid = chunk[int(r["id"])]
                out[mid] = None
                for prop in r["body"].get("singleValueExtendedProperties") or []:
                    try:
                        out[mid] = int(prop.get("value"))
                    except (TypeError, ValueError):
                        pass
        return out

    def message_sizes(self, folder_id: str) -> Iterator[dict[str, int]]:
        """{message id: size in bytes} for a folder, a page (up to 500) at a time, newest
        first; stop iterating when you have what you need."""
        url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages"
        params = {"$select": "id", "$top": 500, "$orderby": "receivedDateTime desc",
                  "$expand": f"singleValueExtendedProperties($filter=id eq '{PROP_SIZE}')"}
        while url:
            data = self._request(url, params=params, headers=_prefer())
            sizes = {}
            for item in data.get("value", []):
                for prop in item.get("singleValueExtendedProperties") or []:
                    try:
                        sizes[item["id"]] = int(prop.get("value"))
                    except (TypeError, ValueError):
                        pass
            yield sizes
            url, params = data.get("@odata.nextLink"), None

    def messages_before(self, folder_id: str, before: str, limit: int = 500) -> list[dict]:
        """Up to `limit` messages received before `before` (ISO time), newest first: mail
        older than the sync period, fetched on request."""
        params = {"$select": MESSAGE_FIELDS, "$top": 100,
                  "$filter": f"receivedDateTime lt {before}",
                  "$orderby": "receivedDateTime desc"}
        url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages"
        out: list[dict] = []
        for item in self._paged(url, params=params, headers=_prefer()):
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def search_messages(self, folder_id: str, words: list[str], limit: int = 250) -> list[dict]:
        """Server-side search of one folder (subject, body, people, attachments' names).
        Every word must match. Newest first as Exchange ranks them; at most `limit` results."""
        terms = []
        for w in words:
            w = w.replace('"', "").replace("\\", "").strip()
            if w:  # lower case, so a word like AND/OR/NOT isn't read as an operator
                terms.append(w.lower() if w.upper() in ("AND", "OR", "NOT") else w)
        if not terms:
            return []
        url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages"
        params = {"$search": '"' + " AND ".join(terms) + '"', "$select": MESSAGE_FIELDS,
                  "$top": 50}
        out: list[dict] = []
        for item in self._paged(url, params=params, headers=_prefer()):
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def update_message(self, message_id: str, changes: dict) -> None:
        """PATCH writable message properties, e.g. {"isRead": True}."""
        self._request(f"{self._root}/messages/{quote(message_id, safe='')}", method="PATCH",
                      json=changes, params={"$select": "id"})

    def move_message(self, message_id: str, destination_id: str) -> dict:
        """Move within this mailbox. With immutable ids the message keeps its id."""
        return self._request(f"{self._root}/messages/{quote(message_id, safe='')}/move",
                             method="POST", json={"destinationId": destination_id},
                             headers=_prefer())

    def delete_message(self, message_id: str) -> None:
        """Delete for good, as Outlook's Shift+Delete or deleting in Deleted Items does: the
        message goes to Recoverable Items, where Outlook's "Recover deleted items" (or an
        administrator) can still get it back for a while."""
        self._request(f"{self._root}/messages/{quote(message_id, safe='')}", method="DELETE")

    def folder_message_ids(self, folder_id: str) -> list[str]:
        """The ids of every message in a folder on the server (not only the synced ones)."""
        return [m["id"] for m in self._paged(
            f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages",
            params={"$select": "id", "$top": 1000})]

    def delete_messages(self, message_ids: list[str], progress=None) -> list[str]:
        """Delete many messages for good, 20 per $batch; `progress(done, total)` after each.
        Returns the ids the server refused (gone already counts as deleted)."""
        refused: list[str] = []
        for i in range(0, len(message_ids), 20):
            chunk = message_ids[i:i + 20]
            for attempt in range(4):  # throttled or failed ones are tried again
                reqs = [{"id": str(n), "method": "DELETE",
                         "url": f"{self._root}/messages/{quote(mid, safe='')}"}
                        for n, mid in enumerate(chunk)]
                data = self._request("/$batch", method="POST", json={"requests": reqs})
                again, wait = [], 0.0
                for r in data.get("responses", []):
                    status, mid = r.get("status", 500), chunk[int(r["id"])]
                    if status in (429, 500, 502, 503, 504) and attempt < 3:
                        again.append(mid)
                        wait = max(wait, float((r.get("headers") or {}).get("Retry-After",
                                                                             2 ** attempt)))
                    elif status >= 300 and status != 404:
                        refused.append(mid)
                if not again:
                    break
                self._sleep(min(wait, 60))
                chunk = again
            if progress is not None:
                progress(min(i + 20, len(message_ids)), len(message_ids))
        return refused

    def copy_message(self, message_id: str, destination_id: str) -> dict:
        """Copy within this mailbox; returns the new message (with a new id)."""
        return self._request(f"{self._root}/messages/{quote(message_id, safe='')}/copy",
                             method="POST", json={"destinationId": destination_id},
                             headers=_prefer())

    # -- attachments (of a message or an event) -------------------------------------
    # `item` is the item's path, from message_path() / event_path().
    SMALL_ATTACHMENT = 3 * 1024 * 1024    # below this: one request; above: an upload session
    MAX_ATTACHMENT = 150 * 1024 * 1024    # Graph's limit per file
    UPLOAD_CHUNK = 12 * 320 * 1024        # < 4 MB per PUT, a multiple of 320 KiB

    def message_path(self, message_id: str) -> str:
        return f"{self._root}/messages/{quote(message_id, safe='')}"

    def event_path(self, event_id: str) -> str:
        return f"{self._root}/events/{quote(event_id, safe='')}"

    def attachments(self, item: str) -> list[dict]:
        """id, name, size, contentType, isInline of each attachment (not the content)."""
        return list(self._paged(f"{item}/attachments", params={
            "$select": "id,name,size,contentType,isInline"}, headers=_prefer()))

    def attachment_content(self, item: str, attachment_id: str) -> bytes:
        """The file itself (an attached message comes as its MIME text, an .eml)."""
        return self._request(f"{item}/attachments/{quote(attachment_id, safe='')}/$value",
                             raw=True)

    def inline_images(self, item: str, wanted: set[str]) -> dict[str, tuple[str, bytes]]:
        """The pictures the body of `item` (message_path / event_path) links to with cid:
        (`wanted`, as cid_key()s): {content id: (type, bytes)}. Inline attachments are tried
        first, then other pictures (some senders don't mark them inline); each is fetched
        whole, the very large skipped."""
        listed = self.attachments(item)
        candidates = [a for a in listed if a.get("isInline")] + [
            a for a in listed if not a.get("isInline")
            and (a.get("contentType") or "").startswith("image/")]
        found: dict[str, tuple[str, bytes]] = {}
        for att in candidates:
            if not wanted - found.keys():
                break
            if int(att.get("size") or 0) > MAX_INLINE_IMAGE:
                continue
            full = self._request(f"{item}/attachments/{quote(att['id'], safe='')}")
            if not full.get("contentBytes"):
                continue  # (an attached message or a link: not a picture)
            picture = (image_type(full.get("contentType"), full.get("name")),
                       base64.b64decode(full["contentBytes"]))
            for key in (full.get("contentId"), full.get("name")):
                if key and cid_key(key) in wanted:
                    found[cid_key(key)] = picture
        return found

    def delete_attachment(self, item: str, attachment_id: str) -> None:
        self._request(f"{item}/attachments/{quote(attachment_id, safe='')}", method="DELETE")

    def add_attachment(self, item: str, name: str, data: bytes,
                       content_type: str = "application/octet-stream",
                       content_id: str | None = None) -> str | None:
        """Attach a file; returns the attachment's id (None if the server didn't say). With
        `content_id` it's an inline picture, which the body shows with a cid: link."""
        inline = {"isInline": True, "contentId": content_id} if content_id else {}
        if len(data) > self.MAX_ATTACHMENT:
            raise GraphError(f"{name} is larger than 150 MB, the most an attachment can be")
        if len(data) < self.SMALL_ATTACHMENT:
            made = self._request(f"{item}/attachments", method="POST", json={
                "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(data).decode("ascii"), **inline})
            return made.get("id")
        session = self._request(f"{item}/attachments/createUploadSession", method="POST",
                                json={"AttachmentItem": {"attachmentType": "file",
                                                         "name": name, "size": len(data),
                                                         **inline}})
        return self._upload(session["uploadUrl"], data)

    def _upload(self, url: str, data: bytes) -> str | None:
        """PUT the bytes to an upload session, in order, a chunk at a time. The URL carries
        its own authorisation: no Authorization header (as Microsoft asks)."""
        total, start = len(data), 0
        while start < total:
            chunk = data[start:start + self.UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            headers = {"Content-Type": "application/octet-stream",
                       "Content-Length": str(len(chunk)),
                       "Content-Range": f"bytes {start}-{end}/{total}"}
            for attempt in range(4):
                try:
                    r = self._s.request("PUT", url, data=chunk, headers=headers, timeout=120)
                except requests.RequestException as exc:
                    _report(OFFLINE, _network_problem(exc))
                    raise
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    self._sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 30))
                    continue
                break
            _report(ONLINE)
            if r.status_code == 201:  # the last piece: the Location names the attachment
                m = re.search(r"Attachments\('([^']+)'\)", r.headers.get("Location", ""))
                return m.group(1) if m else None
            if not r.ok:
                raise GraphError(f"upload {r.status_code}: {r.text[:200]}", r.status_code)
            start = end + 1
        return None

    # -- sending ------------------------------------------------------------
    def send_mail(self, message: dict) -> None:
        """Send a new message (a copy is kept in Sent Items)."""
        self._request(f"{self._root}/sendMail", method="POST",
                      json={"message": message, "saveToSentItems": True})

    def create_draft(self, message: dict) -> dict:
        """Save a new message in Drafts; returns it (with its id)."""
        return self._request(f"{self._root}/messages", method="POST", json=message,
                             headers=_prefer())

    def create_response(self, message_id: str, kind: str) -> dict:
        """A reply / reply-all / forward draft of a message (`kind`: reply, replyAll,
        forward). Exchange sets the threading headers and, for a forward, copies the
        attachments; we then fill in the body and recipients."""
        action = {"reply": "createReply", "replyAll": "createReplyAll",
                  "forward": "createForward"}[kind]
        return self._request(f"{self._root}/messages/{quote(message_id, safe='')}/{action}",
                             method="POST", headers=_prefer())

    def send_draft(self, draft_id: str) -> None:
        self._request(f"{self._root}/messages/{quote(draft_id, safe='')}/send", method="POST")

    def get_message_body(self, message_id: str) -> tuple[str, str]:
        """Return (content_type, content); content_type is 'html' or 'text'."""
        data = self._request(
            f"{self._root}/messages/{quote(message_id, safe='')}",
            params={"$select": "body"},
            headers=_prefer('outlook.body-content-type="html"'),
        )
        body = data.get("body") or {}
        return (body.get("contentType") or "html").lower(), body.get("content") or ""
