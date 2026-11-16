"""Mailboxes: each signed-in account's own mailbox, plus shared mailboxes/folders it opens.

Every mailbox gets its own Graph client (rooted at /me or /users/{address}), sync engine and
body service. They share the store and the account's token.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .auth import Authenticator
from .bodies import BodyService
from .calendars import CalendarSync
from .contacts import ContactSync
from .graph import GraphClient
from .store import Store
from .sync import SyncEngine
from .tasks import TaskSync

log = logging.getLogger(__name__)


@dataclass
class Mailbox:
    id: str
    account_id: str
    address: str | None  # None = the account's own mailbox
    display_name: str
    graph: GraphClient
    sync: SyncEngine
    bodies: BodyService
    contacts: ContactSync | None = None  # an account's own mailbox only
    calendar: CalendarSync | None = None   # likewise
    tasks: TaskSync | None = None          # likewise

    @property
    def shared(self) -> bool:
        return self.address is not None


def shared_mailbox_id(account_id: str, address: str) -> str:
    return f"{account_id}/{address.lower()}"


class MailboxManager:
    def __init__(self, store: Store, auth: Authenticator) -> None:
        self.store = store
        self.auth = auth
        self._boxes: dict[str, Mailbox] = {}
        # Applied to every mailbox, including ones added later (see MainWindow._apply_settings).
        self.initial_days: int | None = None
        self.max_bytes: int | None = None
        self.reload()

    def reload(self) -> None:
        boxes = {}
        for r in self.store.mailbox_rows():
            box = self._boxes.get(r["id"]) or self._make(r)
            box.display_name = r["display_name"]
            boxes[r["id"]] = box
        self._boxes = boxes

    def _make(self, r) -> Mailbox:
        account_id = r["account_id"]
        graph = GraphClient(lambda: self.auth.get_token(account_id), user=r["address"])
        sync = SyncEngine(self.store, graph, r["id"])
        bodies = BodyService(self.store, graph)
        if self.initial_days is not None:
            sync.initial_days = self.initial_days
        if self.max_bytes is not None:
            bodies.max_bytes = self.max_bytes
        own = not r["address"]
        contacts = ContactSync(self.store, graph, account_id) if own else None
        calendar = CalendarSync(self.store, graph, account_id) if own else None
        tasks = TaskSync(self.store, graph, account_id) if own else None
        return Mailbox(r["id"], account_id, r["address"], r["display_name"], graph, sync, bodies,
                       contacts, calendar, tasks)

    def all(self) -> list[Mailbox]:
        return list(self._boxes.values())

    def get(self, mailbox_id: str) -> Mailbox | None:
        return self._boxes.get(mailbox_id)

    def for_folder(self, folder_id: str) -> Mailbox | None:
        mid = self.store.folder_mailbox(folder_id)
        return self._boxes.get(mid) if mid else None

    def configure(self, initial_days: int, max_bytes: int) -> None:
        self.initial_days, self.max_bytes = initial_days, max_bytes
        for box in self._boxes.values():
            box.sync.initial_days = initial_days
            box.bodies.max_bytes = max_bytes

    # -- changes (these talk to Graph or the browser: run them off the UI thread) -------
    def ensure_accounts(self) -> None:
        """Sign in if nobody is, and make sure every signed-in account has its own mailbox."""
        accounts = self.auth.accounts()
        if not accounts:
            self.auth.get_token()  # first run: opens the browser
            accounts = self.auth.accounts()
        first = not self.store.mailbox_rows()
        for acct in accounts:
            self._add_own(acct)
        if first and accounts:
            self.store.adopt_orphan_folders(accounts[0]["home_account_id"])
        self.reload()

    def add_account(self) -> Mailbox:
        acct = self.auth.add_account()
        self._add_own(acct)
        self.reload()
        return self._boxes[acct["home_account_id"]]

    def _add_own(self, acct: dict) -> None:
        aid = acct["home_account_id"]
        if any(r["id"] == aid for r in self.store.mailbox_rows()):
            return
        self.store.add_mailbox(aid, aid, None, acct.get("username") or "Mailbox")

    def add_shared(self, account_id: str, address: str, display_name: str = "") -> Mailbox:
        """Open someone else's mailbox (or the folders they shared) through `account_id`.
        Raises GraphError when nothing in it is accessible."""
        address = address.strip()
        mid = shared_mailbox_id(account_id, address)
        self.store.add_mailbox(mid, account_id, address, display_name.strip() or address)
        self.reload()
        box = self._boxes[mid]
        try:
            box.sync.sync_folders()
        except Exception:
            self.store.remove_mailbox(mid)
            self.reload()
            raise
        return box

    def remove(self, mailbox_id: str) -> None:
        """Remove a shared mailbox, or sign an account out along with its shared mailboxes."""
        box = self._boxes.get(mailbox_id)
        if box is None:
            return
        if box.shared:
            self.store.remove_mailbox(mailbox_id)
        else:
            for other in self.all():
                if other.account_id == box.account_id:
                    self.store.remove_mailbox(other.id)
            self.auth.sign_out(box.account_id)
        self.reload()
