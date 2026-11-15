"""Delta sync of folders and message metadata (bodies are fetched lazily elsewhere)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from . import config
from .graph import DeltaResetError, GraphClient, GraphError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    upserted: int = 0
    removed: int = 0
    full: bool = False
    # Messages that arrived since the last sync (empty on a first or full sync, which
    # downloads everything and so can't tell what is new).
    new_ids: list[str] = field(default_factory=list)


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
        """Send queued read/flag/move changes. A change the server refuses (no write access to a
        shared mailbox, message deleted) is undone locally; network or server errors leave it
        queued for the next try, but don't hold up the changes behind them (unless the network
        is down: then stop). Returns how many were sent."""
        sent = 0
        failed: GraphError | None = None
        for ch in self.store.pending_changes(self.mailbox_id):
            try:
                if ch["field"] == "move":
                    self.graph.move_message(ch["message_id"], ch["value"])
                elif ch["field"] == "isRead":
                    self.graph.update_message(ch["message_id"], {"isRead": ch["value"] == "1"})
                else:
                    self.graph.update_message(ch["message_id"],
                                              {"flag": {"flagStatus": ch["value"]}})
            except GraphError as exc:
                if exc.status in (400, 403, 404):
                    log.warning("server refused %s on %s: %s", ch["field"], ch["message_id"], exc)
                    self.store.change_refused(ch["id"])
                else:  # server trouble: keep it queued, try the rest
                    failed = exc
                continue
            self.store.change_sent(ch["id"])
            sent += 1
        if failed is not None:
            raise failed
        return sent

    def sync_folder(self, folder_id: str) -> SyncResult:
        try:
            self.send_changes()  # first, so the delta below already reflects them
        except Exception as exc:  # they stay queued; a stuck change mustn't block syncing
            log.warning("sending queued changes failed: %s", exc)
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
            up, rm = self.store.apply_message_changes(
                folder_id, items, None if full else result.new_ids)
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
