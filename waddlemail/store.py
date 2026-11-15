"""SQLite store: mailboxes, folder tree, message metadata, LRU body cache, sync cursors."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .graph import PROP_LAST_VERB, PROP_SIZE
from .text import body_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,       -- MSAL home_account_id of the account that opens it
    address TEXT,                   -- NULL = the account's own mailbox; else a shared one
    display_name TEXT NOT NULL,
    position INTEGER DEFAULT 0,
    collapsed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS folders(
    id TEXT PRIMARY KEY,
    mailbox_id TEXT,
    parent_id TEXT,
    well_known TEXT,
    name TEXT NOT NULL,
    child_count INTEGER DEFAULT 0,
    unread INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    collapsed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
    id TEXT PRIMARY KEY,
    folder_id TEXT NOT NULL,
    conversation_id TEXT,
    subject TEXT,
    sender_name TEXT,
    sender_addr TEXT,
    received TEXT,
    is_read INTEGER DEFAULT 0,
    has_attachments INTEGER DEFAULT 0,
    preview TEXT,
    to_recipients TEXT,             -- JSON [[name, address], ...]
    cc_recipients TEXT,
    importance TEXT,                -- low / normal / high
    flag TEXT,                      -- notFlagged / flagged / complete
    last_verb INTEGER,              -- 102 replied, 103 replied all, 104 forwarded
    size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_folder_received
    ON messages(folder_id, received DESC);
CREATE TABLE IF NOT EXISTS bodies(
    message_id TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    content TEXT NOT NULL,
    size INTEGER NOT NULL,
    last_access REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Local changes (read/unread, flag) not yet sent to the server. One per message and field:
-- a newer change replaces an unsent older one. `old` is restored if the server refuses it.
CREATE TABLE IF NOT EXISTS pending(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    field TEXT NOT NULL,            -- isRead / flag
    value TEXT NOT NULL,
    old TEXT,
    UNIQUE(message_id, field)
);
-- Full-text index of cached bodies; rowid = bodies.rowid. Rows follow the bodies table
-- (put_body inserts, the trigger deletes), so evicted bodies leave the index too.
CREATE VIRTUAL TABLE IF NOT EXISTS body_fts USING fts5(
    message_id UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS bodies_fts_delete AFTER DELETE ON bodies BEGIN
    DELETE FROM body_fts WHERE rowid = old.rowid;
END;
"""

# Message columns added after the first release; adding them resets the delta cursors so
# the next sync fetches the new fields for mail already stored.
_NEW_MESSAGE_COLUMNS = {
    "to_recipients": "TEXT", "cc_recipients": "TEXT", "importance": "TEXT", "flag": "TEXT",
    "last_verb": "INTEGER", "size": "INTEGER",
}

_UPSERT_MESSAGE = """
INSERT INTO messages(id, folder_id, conversation_id, subject, sender_name, sender_addr,
                     received, is_read, has_attachments, preview, to_recipients, cc_recipients,
                     importance, flag, last_verb, size)
VALUES(:id, :folder_id, :conversation_id, :subject, :sender_name, :sender_addr,
       :received, COALESCE(:is_read, 0), COALESCE(:has_attachments, 0), :preview,
       :to_recipients, :cc_recipients, :importance, :flag, :last_verb, :size)
ON CONFLICT(id) DO UPDATE SET
    folder_id       = excluded.folder_id,
    conversation_id = COALESCE(excluded.conversation_id, conversation_id),
    subject         = COALESCE(excluded.subject, subject),
    sender_name     = COALESCE(excluded.sender_name, sender_name),
    sender_addr     = COALESCE(excluded.sender_addr, sender_addr),
    received        = COALESCE(excluded.received, received),
    -- a change still waiting to be sent wins over the server's older value
    is_read         = CASE WHEN EXISTS(SELECT 1 FROM pending p
                                       WHERE p.message_id = :id AND p.field = 'isRead')
                           THEN is_read ELSE COALESCE(:is_read, is_read) END,
    flag            = CASE WHEN EXISTS(SELECT 1 FROM pending p
                                       WHERE p.message_id = :id AND p.field = 'flag')
                           THEN flag ELSE COALESCE(excluded.flag, flag) END,
    has_attachments = COALESCE(:has_attachments, has_attachments),
    preview         = COALESCE(excluded.preview, preview),
    to_recipients   = COALESCE(excluded.to_recipients, to_recipients),
    cc_recipients   = COALESCE(excluded.cc_recipients, cc_recipients),
    importance      = COALESCE(excluded.importance, importance),
    last_verb       = COALESCE(excluded.last_verb, last_verb),
    size            = COALESCE(excluded.size, size)
"""


def _row_from_item(item: dict, folder_id: str) -> dict:
    """Map a Graph message to a row; absent properties become None so a partial
    delta update never blanks existing data."""
    sender = (item.get("from") or {}).get("emailAddress") or {}

    def flag(key: str):
        return None if key not in item else int(bool(item[key]))

    def people(key: str):
        if key not in item:
            return None
        return json.dumps([[(r.get("emailAddress") or {}).get("name") or "",
                            (r.get("emailAddress") or {}).get("address") or ""]
                           for r in item[key] or []])

    def tag(prop_id: str) -> int | None:
        # "Integer 0x0E08" and "Integer 0xe08" name the same property
        try:
            return int(prop_id.split()[-1], 16)
        except (IndexError, ValueError):
            return None

    props = {tag(p.get("id", "")): p.get("value")
             for p in item.get("singleValueExtendedProperties") or []}

    def prop(prop_id: str):
        value = props.get(tag(prop_id))
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    return {
        "id": item["id"],
        "folder_id": item.get("parentFolderId") or folder_id,
        "conversation_id": item.get("conversationId"),
        "subject": item.get("subject"),
        "sender_name": sender.get("name"),
        "sender_addr": sender.get("address"),
        "received": item.get("receivedDateTime"),
        "is_read": flag("isRead"),
        "has_attachments": flag("hasAttachments"),
        "preview": item.get("bodyPreview"),
        "to_recipients": people("toRecipients"),
        "cc_recipients": people("ccRecipients"),
        "importance": item.get("importance"),
        "flag": (item.get("flag") or {}).get("flagStatus") if "flag" in item else None,
        "last_verb": prop(PROP_LAST_VERB),
        "size": prop(PROP_SIZE),
    }


def recipients(value: str | None) -> list[tuple[str, str]]:
    """Decode a to_recipients/cc_recipients column into [(name, address)]."""
    try:
        return [tuple(r) for r in json.loads(value)] if value else []
    except (ValueError, TypeError):
        return []


_SEARCH_COLUMNS = ("subject", "sender_name", "sender_addr", "to_recipients", "cc_recipients",
                   "preview")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            had_fts = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='body_fts'").fetchone()
            self.conn.executescript(SCHEMA)
            self._migrate()
            if not had_fts:  # bodies cached before the index existed
                self._index_bodies()

    def _index_bodies(self) -> None:
        rows = self.conn.execute(
            "SELECT rowid, message_id, content_type, content FROM bodies").fetchall()
        self.conn.executemany(
            "INSERT INTO body_fts(rowid, message_id, text) VALUES(?,?,?)",
            ((r["rowid"], r["message_id"], body_text(r["content_type"], r["content"]))
             for r in rows))

    def _migrate(self) -> None:
        # Databases from before multi-mailbox support lack these folder columns; their
        # folders get a mailbox_id from adopt_orphan_folders() once the account is known.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(folders)")}
        for col in ("mailbox_id", "well_known"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE folders ADD COLUMN {col} TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        missing = {c: t for c, t in _NEW_MESSAGE_COLUMNS.items() if c not in cols}
        for col, sqltype in missing.items():
            self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {sqltype}")
        if missing:
            self.conn.execute("DELETE FROM sync_state WHERE key LIKE 'delta:%'")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    # -- mailboxes -----------------------------------------------------
    def mailbox_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mailboxes ORDER BY position, display_name COLLATE NOCASE").fetchall()

    def add_mailbox(self, mailbox_id: str, account_id: str, address: str | None,
                    display_name: str) -> None:
        with self.conn as c:
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM mailboxes").fetchone()[0]
            c.execute(
                """INSERT INTO mailboxes(id, account_id, address, display_name, position)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name""",
                (mailbox_id, account_id, address, display_name, pos),
            )

    def remove_mailbox(self, mailbox_id: str) -> None:
        """Forget a mailbox and everything synced from it."""
        with self.conn as c:
            fids = [r["id"] for r in c.execute(
                "SELECT id FROM folders WHERE mailbox_id=?", (mailbox_id,))]
            for fid in fids:
                c.execute("DELETE FROM messages WHERE folder_id=?", (fid,))
                c.execute("DELETE FROM sync_state WHERE key=?", (f"delta:{fid}",))
            c.execute("DELETE FROM folders WHERE mailbox_id=?", (mailbox_id,))
            c.execute("DELETE FROM pending WHERE mailbox_id=?", (mailbox_id,))
            c.execute("DELETE FROM mailboxes WHERE id=?", (mailbox_id,))
        self.purge_orphan_bodies()

    def set_mailbox_collapsed(self, mailbox_id: str, collapsed: bool) -> None:
        with self.conn as c:
            c.execute("UPDATE mailboxes SET collapsed=? WHERE id=?", (int(collapsed), mailbox_id))

    def adopt_orphan_folders(self, mailbox_id: str) -> None:
        """Give folders synced before mailboxes existed to the (first) account's mailbox."""
        with self.conn as c:
            c.execute("UPDATE folders SET mailbox_id=? WHERE mailbox_id IS NULL", (mailbox_id,))

    # -- folders -------------------------------------------------------
    def upsert_folders(self, mailbox_id: str, folders: list[dict]) -> None:
        """Replace one mailbox's folder list; folders gone from it are dropped with their mail."""
        ids = {f["id"] for f in folders}
        with self.conn as c:
            for f in folders:
                c.execute(
                    """INSERT INTO folders(id, mailbox_id, parent_id, well_known, name,
                                           child_count, unread, total)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET mailbox_id=excluded.mailbox_id,
                         parent_id=excluded.parent_id, well_known=excluded.well_known,
                         name=excluded.name, child_count=excluded.child_count,
                         unread=excluded.unread, total=excluded.total""",
                    (f["id"], mailbox_id, f.get("parentFolderId"), f.get("wellKnownName"),
                     f.get("displayName", "?"), f.get("childFolderCount", 0),
                     f.get("unreadItemCount", 0), f.get("totalItemCount", 0)),
                )
            gone = [r["id"] for r in c.execute(
                "SELECT id FROM folders WHERE mailbox_id=?", (mailbox_id,)) if r["id"] not in ids]
            for fid in gone:
                c.execute("DELETE FROM messages WHERE folder_id=?", (fid,))
                c.execute("DELETE FROM sync_state WHERE key=?", (f"delta:{fid}",))
                c.execute("DELETE FROM folders WHERE id=?", (fid,))

    def folder_rows(self, mailbox_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM folders"
        args: tuple = ()
        if mailbox_id is not None:
            sql, args = sql + " WHERE mailbox_id=?", (mailbox_id,)
        return self.conn.execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()

    def folder_row(self, folder_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def folder_mailbox(self, folder_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT mailbox_id FROM folders WHERE id=?", (folder_id,)).fetchone()
        return row["mailbox_id"] if row else None

    def well_known_folder(self, mailbox_id: str, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM folders WHERE mailbox_id=? AND well_known=?", (mailbox_id, name)
        ).fetchone()
        return row["id"] if row else None

    def set_folder_collapsed(self, folder_id: str, collapsed: bool) -> None:
        with self.conn as c:
            c.execute("UPDATE folders SET collapsed=? WHERE id=?", (int(collapsed), folder_id))

    # -- messages ------------------------------------------------------
    def apply_message_changes(self, folder_id: str, items: list[dict]) -> tuple[int, int]:
        """Apply one page of delta results. Returns (upserted, removed)."""
        up = rm = 0
        with self.conn as c:
            for item in items:
                if "@removed" in item:
                    # Only remove if it still lives in this folder: a message that was
                    # moved elsewhere may already have been re-homed by another folder's sync.
                    cur = c.execute("DELETE FROM messages WHERE id=? AND folder_id=?",
                                    (item["id"], folder_id))
                    rm += cur.rowcount
                else:
                    c.execute(_UPSERT_MESSAGE, _row_from_item(item, folder_id))
                    up += 1
        return up, rm

    def prune_folder(self, folder_id: str, keep_ids: set[str]) -> int:
        """Delete messages in a folder that were not seen during a full resync."""
        with self.conn as c:
            c.execute("CREATE TEMP TABLE IF NOT EXISTS keep(id TEXT PRIMARY KEY)")
            c.execute("DELETE FROM keep")
            c.executemany("INSERT OR IGNORE INTO keep(id) VALUES(?)", ((i,) for i in keep_ids))
            cur = c.execute(
                "DELETE FROM messages WHERE folder_id=? AND id NOT IN (SELECT id FROM keep)",
                (folder_id,),
            )
            return cur.rowcount

    def list_messages(self, folder_id: str, limit: int = 5000, offset: int = 0,
                      since: str | None = None, search: str = "", unread: bool = False,
                      flagged: bool = False, attachments: bool = False,
                      also_ids: list[str] | None = None) -> list[sqlite3.Row]:
        """Newest first. `since` (ISO timestamp) hides older mail without deleting it.

        `search` keeps messages where every word appears in the sender, recipients, subject or
        preview (anywhere in the text), or starts a word in the cached body. While searching,
        `since` is ignored (a search should find old mail too), and `also_ids` (server search
        hits) are kept as well. The flags narrow the list further.
        """
        sql = ("SELECT m.*, (b.message_id IS NOT NULL) AS has_body "
               "FROM messages m LEFT JOIN bodies b ON b.message_id = m.id "
               "WHERE m.folder_id=?")
        args: list = [folder_id]
        words = search.split()
        if since and not words:
            sql += " AND m.received >= ?"
            args.append(since)
        if words:
            local, local_args = [], []
            for word in words:
                like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace(
                    "_", "\\_") + "%"
                alts = [f"m.{col} LIKE ? ESCAPE '\\'" for col in _SEARCH_COLUMNS]
                local_args.extend([like] * len(_SEARCH_COLUMNS))
                if any(ch.isalnum() for ch in word):  # FTS can't match pure punctuation
                    alts.append("m.id IN (SELECT message_id FROM body_fts WHERE body_fts MATCH ?)")
                    local_args.append('"' + word.replace('"', '""') + '"*')
                local.append("(" + " OR ".join(alts) + ")")
            clause = " AND ".join(local)
            if also_ids:
                clause = f"({clause}) OR m.id IN (SELECT value FROM json_each(?))"
                local_args.append(json.dumps(also_ids))
            sql += f" AND ({clause})"
            args.extend(local_args)
        if unread:
            sql += " AND m.is_read = 0"
        if flagged:
            sql += " AND m.flag = 'flagged'"
        if attachments:
            sql += " AND m.has_attachments = 1"
        sql += " ORDER BY m.received DESC LIMIT ? OFFSET ?"
        return self.conn.execute(sql, (*args, limit, offset)).fetchall()

    def message_row(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()

    # -- local changes, queued for the server ---------------------------
    def set_read(self, mailbox_id: str, message_ids: list[str], read: bool) -> list[str]:
        """Mark messages read/unread locally and queue the change. Returns the ids that changed."""
        changed = []
        with self.conn as c:
            for mid in message_ids:
                row = c.execute("SELECT folder_id, is_read FROM messages WHERE id=?",
                                (mid,)).fetchone()
                if row is None or bool(row["is_read"]) == read:
                    continue
                c.execute("UPDATE messages SET is_read=? WHERE id=?", (int(read), mid))
                c.execute("UPDATE folders SET unread = MAX(unread + ?, 0) WHERE id=?",
                          (-1 if read else 1, row["folder_id"]))
                self._queue(c, mailbox_id, mid, "isRead", "1" if read else "0",
                            "1" if row["is_read"] else "0")
                changed.append(mid)
        return changed

    def set_flag(self, mailbox_id: str, message_ids: list[str], status: str) -> list[str]:
        """Set flagStatus (notFlagged / flagged / complete) locally and queue the change."""
        changed = []
        with self.conn as c:
            for mid in message_ids:
                row = c.execute("SELECT flag FROM messages WHERE id=?", (mid,)).fetchone()
                old = (row["flag"] or "notFlagged") if row else None
                if row is None or old == status:
                    continue
                c.execute("UPDATE messages SET flag=? WHERE id=?", (status, mid))
                self._queue(c, mailbox_id, mid, "flag", status, old)
                changed.append(mid)
        return changed

    @staticmethod
    def _queue(c, mailbox_id, message_id, field, value, old):
        prev = c.execute("SELECT old FROM pending WHERE message_id=? AND field=?",
                         (message_id, field)).fetchone()
        if prev is not None:
            old = prev["old"]  # still unsent: the server has the oldest value
            if old == value:   # changed back before it was sent: nothing to send
                c.execute("DELETE FROM pending WHERE message_id=? AND field=?",
                          (message_id, field))
                return
        c.execute("INSERT OR REPLACE INTO pending(mailbox_id, message_id, field, value, old)"
                  " VALUES(?,?,?,?,?)", (mailbox_id, message_id, field, value, old))

    def pending_changes(self, mailbox_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pending WHERE mailbox_id=? ORDER BY id",
                                 (mailbox_id,)).fetchall()

    def change_sent(self, change_id: int) -> None:
        with self.conn as c:
            c.execute("DELETE FROM pending WHERE id=?", (change_id,))

    def change_refused(self, change_id: int) -> None:
        """The server rejected a change (no write access, message gone): undo it locally."""
        with self.conn as c:
            row = c.execute("SELECT * FROM pending WHERE id=?", (change_id,)).fetchone()
            if row is None:
                return
            c.execute("DELETE FROM pending WHERE id=?", (change_id,))
            if row["old"] is None:
                return
            if row["field"] == "isRead":
                c.execute("UPDATE messages SET is_read=? WHERE id=?",
                          (int(row["old"]), row["message_id"]))
            else:
                c.execute("UPDATE messages SET flag=? WHERE id=?", (row["old"], row["message_id"]))

    def message_count(self, folder_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE folder_id=?", (folder_id,)
        ).fetchone()[0]

    # -- bodies (LRU cache) -------------------------------------------
    def get_body(self, message_id: str) -> tuple[str, str] | None:
        with self.conn as c:
            row = c.execute(
                "SELECT content_type, content FROM bodies WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE bodies SET last_access=? WHERE message_id=?",
                      (time.time(), message_id))
        return row["content_type"], row["content"]

    def put_body(self, message_id: str, content_type: str, content: str, max_bytes: int) -> None:
        size = len(content.encode("utf-8"))
        text = body_text(content_type, content)
        with self.conn as c:
            # DELETE + INSERT rather than REPLACE, so the trigger drops the old index row
            c.execute("DELETE FROM bodies WHERE message_id=?", (message_id,))
            cur = c.execute(
                "INSERT INTO bodies(message_id, content_type, content, size, last_access)"
                " VALUES(?,?,?,?,?)",
                (message_id, content_type, content, size, time.time()),
            )
            c.execute("INSERT INTO body_fts(rowid, message_id, text) VALUES(?,?,?)",
                      (cur.lastrowid, message_id, text))
            total = c.execute("SELECT COALESCE(SUM(size),0) FROM bodies").fetchone()[0]
            while total > max_bytes:
                oldest = c.execute(
                    "SELECT message_id, size FROM bodies WHERE message_id != ? "
                    "ORDER BY last_access ASC LIMIT 1", (message_id,)
                ).fetchone()
                if oldest is None:
                    break
                c.execute("DELETE FROM bodies WHERE message_id=?", (oldest["message_id"],))
                total -= oldest["size"]

    def body_cache_size(self) -> int:
        return self.conn.execute("SELECT COALESCE(SUM(size),0) FROM bodies").fetchone()[0]

    def purge_orphan_bodies(self) -> int:
        with self.conn as c:
            cur = c.execute(
                "DELETE FROM bodies WHERE message_id NOT IN (SELECT id FROM messages)"
            )
            return cur.rowcount

    def clear_bodies(self) -> None:
        with self.conn as c:
            c.execute("DELETE FROM bodies")

    def purge_bodies_older_than(self, days: int) -> int:
        """Drop cached bodies that have not been opened for `days` days."""
        cutoff = time.time() - days * 86400
        with self.conn as c:
            return c.execute("DELETE FROM bodies WHERE last_access < ?", (cutoff,)).rowcount

    # -- sync cursors --------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES(?,?)", (key, value))

    def delete_state(self, key: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM sync_state WHERE key=?", (key,))
