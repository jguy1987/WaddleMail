"""SQLite store: mailboxes, folder tree, message metadata, LRU body cache, sync cursors."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

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
    preview TEXT
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
"""

_UPSERT_MESSAGE = """
INSERT INTO messages(id, folder_id, conversation_id, subject, sender_name, sender_addr,
                     received, is_read, has_attachments, preview)
VALUES(:id, :folder_id, :conversation_id, :subject, :sender_name, :sender_addr,
       :received, COALESCE(:is_read, 0), COALESCE(:has_attachments, 0), :preview)
ON CONFLICT(id) DO UPDATE SET
    folder_id       = excluded.folder_id,
    conversation_id = COALESCE(excluded.conversation_id, conversation_id),
    subject         = COALESCE(excluded.subject, subject),
    sender_name     = COALESCE(excluded.sender_name, sender_name),
    sender_addr     = COALESCE(excluded.sender_addr, sender_addr),
    received        = COALESCE(excluded.received, received),
    is_read         = COALESCE(:is_read, is_read),
    has_attachments = COALESCE(:has_attachments, has_attachments),
    preview         = COALESCE(excluded.preview, preview)
"""


def _row_from_item(item: dict, folder_id: str) -> dict:
    """Map a Graph message to a row; absent properties become None so a partial
    delta update never blanks existing data."""
    sender = (item.get("from") or {}).get("emailAddress") or {}

    def flag(key: str):
        return None if key not in item else int(bool(item[key]))

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
    }


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        # Databases from before multi-mailbox support lack these folder columns; their
        # folders get a mailbox_id from adopt_orphan_folders() once the account is known.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(folders)")}
        for col in ("mailbox_id", "well_known"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE folders ADD COLUMN {col} TEXT")

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

    def list_messages(self, folder_id: str, limit: int = 500, offset: int = 0,
                      since: str | None = None) -> list[sqlite3.Row]:
        """Newest first. `since` (ISO timestamp) hides older mail without deleting it."""
        sql = ("SELECT m.*, (b.message_id IS NOT NULL) AS has_body "
               "FROM messages m LEFT JOIN bodies b ON b.message_id = m.id "
               "WHERE m.folder_id=?")
        args: list = [folder_id]
        if since:
            sql += " AND m.received >= ?"
            args.append(since)
        sql += " ORDER BY m.received DESC LIMIT ? OFFSET ?"
        return self.conn.execute(sql, (*args, limit, offset)).fetchall()

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
        with self.conn as c:
            c.execute(
                "INSERT OR REPLACE INTO bodies(message_id, content_type, content, size, last_access)"
                " VALUES(?,?,?,?,?)",
                (message_id, content_type, content, size, time.time()),
            )
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
