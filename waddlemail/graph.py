"""Thin Microsoft Graph client: paging, throttling/retry, delta, message bodies."""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterator
from urllib.parse import quote

import requests

from . import config

log = logging.getLogger(__name__)

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
# MAPI properties Graph has no first-class field for: PidTagLastVerbExecuted (102 replied,
# 103 replied to all, 104 forwarded) and PidTagMessageSize (bytes).
PROP_LAST_VERB = "Integer 0x1081"
PROP_SIZE = "Integer 0x0E08"
EXTENDED_PROPS = (f"singleValueExtendedProperties($filter=id eq '{PROP_LAST_VERB}'"
                  f" or id eq '{PROP_SIZE}')")


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
        self._expand_props = True  # turned off if the server refuses the $expand
        self._s = session or requests.Session()
        self._sleep = sleep

    # -- low level -----------------------------------------------------
    def _url(self, url: str) -> str:
        return url if url.startswith("http") else self._base + url

    def _request(self, url: str, params: dict | None = None, headers: dict | None = None,
                 retries: int = 5, method: str = "GET", json: dict | None = None) -> dict:
        full = self._url(url)
        refreshed = False
        for attempt in range(retries):
            hdrs = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
            r = self._s.request(method, full, params=params, headers=hdrs, json=json, timeout=30)
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

    # -- messages ------------------------------------------------------
    def messages_delta(self, folder_id: str, delta_link: str | None = None,
                       since: str | None = None) -> Iterator[tuple[list[dict], str | None]]:
        """Yield (items, delta_link). delta_link is set only on the final page."""
        if delta_link:
            url, params = delta_link, None
        else:
            url = f"{self._root}/mailFolders/{quote(folder_id, safe='')}/messages/delta"
            params = {"$select": MESSAGE_FIELDS}
            if self._expand_props:
                params["$expand"] = EXTENDED_PROPS
            if since:
                params["$filter"] = f"receivedDateTime ge {since}"
        headers = _prefer("odata.maxpageSize=100")
        while True:
            try:
                data = self._request(url, params=params, headers=headers)
            except GraphError as exc:
                if exc.status != 400 or not params or "$expand" not in params:
                    raise
                log.warning("extended properties refused; syncing without reply state/size: %s",
                            exc)
                self._expand_props = False
                params = {k: v for k, v in params.items() if k != "$expand"}
                continue
            nxt = data.get("@odata.nextLink")
            yield data.get("value", []), data.get("@odata.deltaLink")
            if not nxt:
                return
            url, params = nxt, None

    def messages_before(self, folder_id: str, before: str, limit: int = 500) -> list[dict]:
        """Up to `limit` messages received before `before` (ISO time), newest first: mail
        older than the sync period, fetched on request."""
        params = {"$select": MESSAGE_FIELDS, "$top": 100,
                  "$filter": f"receivedDateTime lt {before}",
                  "$orderby": "receivedDateTime desc"}
        if self._expand_props:
            params["$expand"] = EXTENDED_PROPS
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

    def copy_message(self, message_id: str, destination_id: str) -> dict:
        """Copy within this mailbox; returns the new message (with a new id)."""
        return self._request(f"{self._root}/messages/{quote(message_id, safe='')}/copy",
                             method="POST", json={"destinationId": destination_id},
                             headers=_prefer())

    def get_message_body(self, message_id: str) -> tuple[str, str]:
        """Return (content_type, content); content_type is 'html' or 'text'."""
        data = self._request(
            f"{self._root}/messages/{quote(message_id, safe='')}",
            params={"$select": "body"},
            headers=_prefer('outlook.body-content-type="html"'),
        )
        body = data.get("body") or {}
        return (body.get("contentType") or "html").lower(), body.get("content") or ""
