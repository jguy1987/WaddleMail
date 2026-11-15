"""Delta sync of folders and message metadata (bodies are fetched lazily elsewhere)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from . import config
from .graph import DeltaResetError, GraphClient, GraphError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    upserted: int = 0
    removed: int = 0
    full: bool = False


class SyncEngine:
    def __init__(self, store: Store, graph: GraphClient, mailbox_id: str,
                 initial_days: int = config.INITIAL_SYNC_DAYS) -> None:
        self.store = store
        self.graph = graph
        self.mailbox_id = mailbox_id
        self.initial_days = initial_days

    def sync_folders(self) -> None:
        self.store.upsert_folders(self.mailbox_id, self.graph.list_folders())

    def send_changes(self) -> int:
        """Send queued read/flag changes. A change the server refuses (no write access to a
        shared mailbox, message deleted) is undone locally; network or server errors leave it
        queued for the next try. Returns how many were sent."""
        sent = 0
        for ch in self.store.pending_changes(self.mailbox_id):
            if ch["field"] == "isRead":
                body = {"isRead": ch["value"] == "1"}
            else:
                body = {"flag": {"flagStatus": ch["value"]}}
            try:
                self.graph.update_message(ch["message_id"], body)
            except GraphError as exc:
                if exc.status not in (400, 403, 404):
                    raise
                log.warning("server refused %s on %s: %s", ch["field"], ch["message_id"], exc)
                self.store.change_refused(ch["id"])
                continue
            self.store.change_sent(ch["id"])
            sent += 1
        return sent

    def sync_folder(self, folder_id: str) -> SyncResult:
        self.send_changes()  # first, so the delta below already reflects them
        key = f"delta:{folder_id}"
        link = self.store.get_state(key)
        try:
            result = self._run(folder_id, key, link)
        except DeltaResetError:
            log.info("delta link expired for %s; full resync", folder_id)
            self.store.delete_state(key)
            result = self._run(folder_id, key, None)
        self.store.purge_orphan_bodies()
        return result

    def _run(self, folder_id: str, key: str, link: str | None) -> SyncResult:
        full = link is None
        since = None
        if full:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.initial_days)
            since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = SyncResult(full=full)
        seen: set[str] = set()
        final_link: str | None = None
        for items, delta_link in self.graph.messages_delta(folder_id, link, since):
            up, rm = self.store.apply_message_changes(folder_id, items)
            result.upserted += up
            result.removed += rm
            if full:
                seen.update(i["id"] for i in items if "@removed" not in i)
            if delta_link:
                final_link = delta_link
        if full:
            result.removed += self.store.prune_folder(folder_id, seen)
        # Persist the cursor only after every page was applied, so a crash
        # mid-sync simply replays the same (idempotent) changes.
        if final_link:
            self.store.set_state(key, final_link)
        return result
