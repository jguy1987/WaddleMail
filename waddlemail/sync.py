"""Delta sync of folders and message metadata (bodies are fetched lazily elsewhere)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from . import config
from .graph import VERBS, DeltaResetError, GraphClient, GraphError
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
    changed_ids: list[str] = field(default_factory=list)  # stored messages that changed


class SyncEngine:
    def __init__(self, store: Store, graph: GraphClient, mailbox_id: str,
                 initial_days: int = config.INITIAL_SYNC_DAYS) -> None:
        self.store = store
        self.graph = graph
        self.mailbox_id = mailbox_id
        self.initial_days = initial_days
        self.sizes_refused = ""  # set if the server won't send message sizes
        self.verbs_refused = ""  # ... or replied/forwarded marks

    def sync_folders(self) -> None:
        self.store.upsert_folders(self.mailbox_id, self.graph.list_folders())

    def send_changes(self) -> int:
        """Send queued read/flag/move/delete changes. A change the server refuses (no write
        access to a shared mailbox, message deleted) is undone locally; deleting a message
        that's already gone counts as done. Network or server errors leave a change queued for
        the next try, but don't hold up the changes behind it (unless the network is down:
        then stop). Returns how many were sent."""
        sent = 0
        failed: GraphError | None = None
        for ch in self.store.pending_changes(self.mailbox_id):
            try:
                if ch["field"] == "move":
                    self.graph.move_message(ch["message_id"], ch["value"])
                elif ch["field"] == "delete":
                    self.graph.delete_message(ch["message_id"])
                elif ch["field"] == "isRead":
                    self.graph.update_message(ch["message_id"], {"isRead": ch["value"] == "1"})
                else:
                    self.graph.update_message(ch["message_id"],
                                              {"flag": {"flagStatus": ch["value"]}})
            except GraphError as exc:
                if ch["field"] == "delete" and exc.status == 404:
                    self.store.change_sent(ch["id"])  # already gone: that's what we wanted
                elif exc.status in (400, 403, 404):
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
        if link and self._window_grew(folder_id):
            # The cursor only covers the period it was made with: start again to fetch the
            # mail that a longer "Keep mail locally" period now includes.
            log.info("sync period grew for %s; full resync", folder_id)
            self.store.delete_state(key)
            link = None
        try:
            result = self._run(folder_id, key, link)
        except DeltaResetError:
            log.info("delta link expired for %s; full resync", folder_id)
            self.store.delete_state(key)
            result = self._run(folder_id, key, None)
        self.store.purge_orphan_bodies()
        self._update_verbs(folder_id, result)
        return result

    # -- replied / forwarded ---------------------------------------------------------
    def _update_verbs(self, folder_id: str, result: SyncResult) -> None:
        """Keep the replied/forwarded marks current (the delta can't bring them): a
        back-fill the first time (and after a full sync), then a look at whatever changed.
        Best effort: a failure here never fails the sync."""
        key = f"verbs:{folder_id}"
        try:
            if result.full or self.store.get_state(key) is None:
                self.backfill_verbs(folder_id)
                self.store.set_state(key, "1")
            elif result.changed_ids:
                self.store.set_verbs(self.graph.message_verbs(result.changed_ids))
        except Exception as exc:  # (incl. network trouble: the next sync tries again)
            log.warning("replied/forwarded marks not updated: %s", exc)
            if isinstance(exc, GraphError) and exc.status == 400:
                self.verbs_refused = str(exc)

    def backfill_verbs(self, folder_id: str) -> int:
        """Mark every stored message of the folder that was replied to or forwarded (and
        clear marks that are no longer so). Returns how many are marked."""
        found: dict[str, int | None] = {}
        for verb in VERBS:
            for mid in self.graph.messages_with_verb(folder_id, verb):
                found[mid] = verb
        stored = self.store.message_ids(folder_id)
        marks = {mid: found.get(mid) for mid in stored}
        self.store.set_verbs(marks)
        return sum(1 for v in marks.values() if v)

    def _since(self) -> str:
        """The oldest receivedDateTime the current sync period covers."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.initial_days)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _window_grew(self, folder_id: str) -> bool:
        """True if the folder's cursor was made for a shorter period than the current one
        (or before periods were recorded)."""
        used = self.store.get_state(f"window:{folder_id}")
        return used is None or self._since() < used

    def fill_sizes(self, folder_id: str, max_pages: int = 4) -> int:
        """Fetch the sizes of the folder's messages that don't have one yet (the delta can't
        bring them; see graph.PROP_LAST_VERB), newest first, up to `max_pages` x 500 per
        call. Costs nothing when every size is known. Returns how many were filled."""
        if self.sizes_refused:
            return 0
        missing = self.store.missing_sizes(folder_id)
        if not missing:
            return 0
        filled = 0
        try:
            for i, page in enumerate(self.graph.message_sizes(folder_id)):
                found = {k: v for k, v in page.items() if k in missing}
                self.store.set_sizes(found)
                filled += len(found)
                missing -= found.keys()
                if not missing or i + 1 >= max_pages:
                    break
        except GraphError as exc:
            if exc.status != 400:
                raise
            log.warning("message sizes refused: %s", exc)
            self.sizes_refused = str(exc)
        return filled

    def fetch_older(self, folder_id: str, limit: int = 500) -> int:
        """Download up to `limit` messages older than the oldest one stored for the folder
        (mail outside the sync period, on request). Returns how many arrived."""
        before = self.store.oldest_received(folder_id) or self._since()
        items = self.graph.messages_before(folder_id, before, limit)
        self.store.apply_message_changes(folder_id, items)
        return len(items)

    def _run(self, folder_id: str, key: str, link: str | None) -> SyncResult:
        full = link is None
        since = self._since() if full else None
        result = SyncResult(full=full)
        seen: set[str] = set()
        final_link: str | None = None
        for items, delta_link in self.graph.messages_delta(folder_id, link, since):
            up, rm = self.store.apply_message_changes(
                folder_id, items, None if full else result.new_ids,
                None if full else result.changed_ids)
            result.upserted += up
            result.removed += rm
            if full:
                seen.update(i["id"] for i in items if "@removed" not in i)
            if delta_link:
                final_link = delta_link
        if full:
            result.removed += self.store.prune_folder(folder_id, seen, since)
        # Persist the cursor only after every page was applied, so a crash
        # mid-sync simply replays the same (idempotent) changes.
        if final_link:
            self.store.set_state(key, final_link)
            if full:
                self.store.set_state(f"window:{folder_id}", since)
        return result
