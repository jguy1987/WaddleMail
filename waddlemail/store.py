"""SQLite store: mailboxes, folder tree, message metadata, LRU body cache, sync cursors."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from email.utils import parseaddr
from pathlib import Path

from .graph import DELTA_QUERY_VERSION, PROP_LAST_VERB, PROP_SIZE
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
    field TEXT NOT NULL,            -- isRead / flag / move (value: destination folder) /
                                    -- delete (deleted for good; the message is hidden until sent)
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
-- Tasks (Microsoft To Do): each account's lists and their tasks (tasks.py).
CREATE TABLE IF NOT EXISTS task_lists(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    well_known TEXT,                -- defaultList / flaggedEmails / none
    shared INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    list_id TEXT NOT NULL,
    title TEXT,
    status TEXT,                    -- notStarted / inProgress / waitingOnOthers / deferred / completed
    importance TEXT,                -- low / normal / high
    due TEXT,                       -- YYYY-MM-DD
    start TEXT,                     -- YYYY-MM-DD
    reminder TEXT,                  -- UTC ISO; NULL = no reminder
    completed TEXT,                 -- UTC ISO
    body TEXT, body_type TEXT,
    categories TEXT,                -- JSON [name, ...]
    created TEXT, modified TEXT,
    links TEXT,                     -- JSON [[name, web url, external id, app], ...]; NULL = not fetched
    recurrence TEXT                 -- JSON patternedRecurrence
);
CREATE INDEX IF NOT EXISTS idx_tasks_list ON tasks(list_id);
-- Messages waiting to be sent (outbox.py): the server couldn't be reached when you sent them.
CREATE TABLE IF NOT EXISTS outbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id TEXT NOT NULL,
    created TEXT NOT NULL,          -- ISO UTC: when you sent it
    payload TEXT NOT NULL,          -- JSON (outbox.make_payload), updated after each try
    error TEXT,                     -- why the last try failed
    refused INTEGER DEFAULT 0       -- 1: the server refused it: waits for you, not retried
);
-- Pictures a cached body shows with cid: links (its inline attachments), by content id
-- (cid_key). They count toward the body's size in the cache, and leave with it.
CREATE TABLE IF NOT EXISTS inline_images(
    message_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY(message_id, content_id)
);
CREATE TRIGGER IF NOT EXISTS bodies_images_delete AFTER DELETE ON bodies BEGIN
    DELETE FROM inline_images WHERE message_id = old.message_id;
END;
-- People: personal contacts (any contact folder) and the organization directory.
CREATE TABLE IF NOT EXISTS contacts(
    id TEXT PRIMARY KEY,            -- Graph contact id, or "dir:" + directory user id
    account_id TEXT NOT NULL,
    source TEXT NOT NULL,           -- personal / directory
    folder_id TEXT,                 -- contact folder ("" = the default Contacts folder)
    folder_name TEXT,
    display_name TEXT NOT NULL,
    given_name TEXT, surname TEXT,
    company TEXT, job_title TEXT, department TEXT, office TEXT,
    emails TEXT,                    -- JSON [[name, address], ...]
    phones TEXT,                    -- JSON [[label, number], ...]
    notes TEXT,
    addresses TEXT,                 -- JSON [[label, multi-line postal address], ...]
    birthday TEXT,                  -- YYYY-MM-DD
    website TEXT,
    nickname TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source, account_id);
-- Personal contact groups (Outlook "contact lists").
CREATE TABLE IF NOT EXISTS contact_groups(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    notes TEXT,
    members TEXT                    -- JSON [[name, address, type, key, memberId], ...]
);
-- Calendars and their events. Timed events: start/end are UTC ISO times; all-day events:
-- start/end are dates (end exclusive), which mean the same day everywhere.
CREATE TABLE IF NOT EXISTS calendars(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT,                     -- #rrggbb
    is_default INTEGER DEFAULT 0,
    can_edit INTEGER DEFAULT 1,
    teams INTEGER DEFAULT 0,        -- Teams meetings can be added
    owner TEXT,
    visible INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    subject TEXT,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    all_day INTEGER DEFAULT 0,
    show_as TEXT,                   -- free / tentative / busy / oof / workingElsewhere
    categories TEXT,                -- JSON [name, ...]
    location TEXT,
    organizer_name TEXT, organizer_addr TEXT,
    is_organizer INTEGER DEFAULT 0,
    attendees TEXT,                 -- JSON [[name, address, required/optional, response], ...]
    online_url TEXT,                -- Teams join link
    preview TEXT,
    response TEXT,                  -- your own: organizer / accepted / tentativelyAccepted / ...
    cancelled INTEGER DEFAULT 0,
    kind TEXT,                      -- singleInstance / occurrence / exception / seriesMaster
    series_id TEXT,
    sensitivity TEXT,
    reminder INTEGER                -- minutes before; NULL = no reminder
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(calendar_id, start);
-- Reminders you dismissed or snoozed, per occurrence (event id + its start).
CREATE TABLE IF NOT EXISTS reminder_state(
    event_id TEXT NOT NULL,
    start TEXT NOT NULL,
    dismissed INTEGER DEFAULT 0,
    snoozed_until TEXT,             -- UTC ISO
    PRIMARY KEY(event_id, start)
);
CREATE TABLE IF NOT EXISTS categories(
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT,                     -- #rrggbb
    PRIMARY KEY(account_id, name)
);
-- Email signatures, one per account.
CREATE TABLE IF NOT EXISTS signatures(
    account_id TEXT PRIMARY KEY,
    html TEXT NOT NULL DEFAULT '',
    on_new INTEGER DEFAULT 1,       -- add to new messages
    on_reply INTEGER DEFAULT 1      -- add to replies and forwards
);
-- Contact photos, fetched when first shown. data NULL: the person has no photo (asked at
-- `fetched`, asked again after a week).
CREATE TABLE IF NOT EXISTS photos(
    id TEXT PRIMARY KEY,            -- contact id, or "dir:" + directory user id
    data BLOB,
    fetched REAL NOT NULL
);
-- People you send to and reply to, for the Recent list and autocomplete.
CREATE TABLE IF NOT EXISTS recipients(
    address TEXT PRIMARY KEY,       -- lower case
    name TEXT,
    sent INTEGER DEFAULT 0,         -- messages you sent them (To/Cc)
    replied INTEGER DEFAULT 0,      -- their messages you replied to
    last_used TEXT
);
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
    folder_id       = CASE WHEN EXISTS(SELECT 1 FROM pending p
                                       WHERE p.message_id = :id AND p.field = 'move')
                           THEN folder_id ELSE excluded.folder_id END,
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


def _people(addresses: list[str]) -> list[list[str]]:
    """"Name <address>" strings as a to_recipients column's [[name, address], ...]."""
    out = []
    for text in addresses:
        name, addr = parseaddr(text)
        if addr:
            out.append([name or addr, addr])
    return out


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
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(contacts)")}
        for col in ("addresses", "birthday", "website", "nickname"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        missing = {c: t for c, t in _NEW_MESSAGE_COLUMNS.items() if c not in cols}
        for col, sqltype in missing.items():
            self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {sqltype}")
        version = self.conn.execute(
            "SELECT value FROM sync_state WHERE key='delta_query'").fetchone()
        if missing or (version or [None])[0] != DELTA_QUERY_VERSION:
            # cursors made with another query (or before these columns existed) would keep
            # leaving fields out: make them again (headers only; bodies and changes stay)
            self.conn.execute("DELETE FROM sync_state WHERE key LIKE 'delta:%'"
                              " OR key LIKE 'window:%'")
            self.conn.execute("INSERT OR REPLACE INTO sync_state(key, value)"
                              " VALUES('delta_query', ?)", (DELTA_QUERY_VERSION,))

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
                c.execute("DELETE FROM sync_state WHERE key IN (?, ?)",
                          (f"delta:{fid}", f"window:{fid}"))
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
                c.execute("DELETE FROM sync_state WHERE key IN (?, ?)",
                          (f"delta:{fid}", f"window:{fid}"))
                c.execute("DELETE FROM folders WHERE id=?", (fid,))

    def folder_rows(self, mailbox_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM folders"
        args: tuple = ()
        if mailbox_id is not None:
            sql, args = sql + " WHERE mailbox_id=?", (mailbox_id,)
        return self.conn.execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()

    def folder_row(self, folder_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def folder_path(self, folder_id: str) -> list[sqlite3.Row]:
        """The folder and its ancestors, top-most first."""
        path, seen = [], set()
        row = self.folder_row(folder_id)
        while row is not None and row["id"] not in seen:
            seen.add(row["id"])
            path.append(row)
            row = self.folder_row(row["parent_id"]) if row["parent_id"] else None
        return path[::-1]

    def folder_subtree(self, folder_id: str) -> list[sqlite3.Row]:
        """The folder and all folders below it."""
        return self.conn.execute(
            """WITH RECURSIVE sub(id) AS (
                   SELECT ? UNION SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id)
               SELECT folders.* FROM folders JOIN sub USING(id)""", (folder_id,)).fetchall()

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
    def apply_message_changes(self, folder_id: str, items: list[dict],
                              new: list[str] | None = None,
                              changed: list[str] | None = None) -> tuple[int, int]:
        """Apply one page of delta results. Returns (upserted, removed); ids of messages
        that weren't stored before are appended to `new`, of ones that were to `changed`."""
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
                    if new is not None or changed is not None:
                        known = c.execute("SELECT 1 FROM messages WHERE id=?",
                                          (item["id"],)).fetchone() is not None
                        target = changed if known else new
                        if target is not None:
                            target.append(item["id"])
                    c.execute(_UPSERT_MESSAGE, _row_from_item(item, folder_id))
                    up += 1
        return up, rm

    def prune_folder(self, folder_id: str, keep_ids: set[str], since: str | None = None) -> int:
        """Delete messages in a folder that were not seen during a full resync of the period
        from `since`. Older mail (downloaded on request, or found by a server search) isn't
        part of that resync, so it stays."""
        with self.conn as c:
            c.execute("CREATE TEMP TABLE IF NOT EXISTS keep(id TEXT PRIMARY KEY)")
            c.execute("DELETE FROM keep")
            c.executemany("INSERT OR IGNORE INTO keep(id) VALUES(?)", ((i,) for i in keep_ids))
            # (a message moved here locally isn't on the server's list yet: keep it)
            cur = c.execute(
                "DELETE FROM messages WHERE folder_id=? AND id NOT IN (SELECT id FROM keep)"
                " AND id NOT IN (SELECT message_id FROM pending WHERE field='move')"
                " AND (? IS NULL OR received IS NULL OR received >= ?)",
                (folder_id, since, since),
            )
            return cur.rowcount

    def list_messages(self, folder_id: str, limit: int = -1, offset: int = 0,
                      since: str | None = None, search: str = "", unread: bool = False,
                      flagged: bool = False, attachments: bool = False,
                      also_ids: list[str] | None = None) -> list[sqlite3.Row]:
        """Newest first; `limit` -1 means no limit. `since` (ISO timestamp) hides older mail
        without deleting it.

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

    def set_verbs(self, verbs: dict[str, int | None]) -> None:
        """Replied (102) / replied to all (103) / forwarded (104), or None, per message."""
        with self.conn as c:
            c.executemany("UPDATE messages SET last_verb=? WHERE id=?",
                          [(v, k) for k, v in verbs.items()])

    def message_ids(self, folder_id: str) -> set[str]:
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM messages WHERE folder_id=?", (folder_id,))}

    def missing_sizes(self, folder_id: str) -> set[str]:
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM messages WHERE folder_id=? AND size IS NULL", (folder_id,))}

    def set_sizes(self, sizes: dict[str, int]) -> None:
        with self.conn as c:
            c.executemany("UPDATE messages SET size=? WHERE id=?",
                          [(v, k) for k, v in sizes.items()])

    def mark_responded(self, message_id: str, verb: int) -> None:
        """Mark a message replied (102), replied to all (103) or forwarded (104) locally, as
        Exchange does when the reply goes out (the next sync confirms it)."""
        with self.conn as c:
            c.execute("UPDATE messages SET last_verb=? WHERE id=?", (verb, message_id))

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

    def move_messages(self, mailbox_id: str, message_ids: list[str], dest: str) -> list[str]:
        """Move messages to folder `dest` locally (counts included) and queue the move."""
        changed = []
        with self.conn as c:
            for mid in message_ids:
                row = c.execute("SELECT folder_id, is_read FROM messages WHERE id=?",
                                (mid,)).fetchone()
                if row is None or row["folder_id"] == dest:
                    continue
                c.execute("UPDATE messages SET folder_id=? WHERE id=?", (dest, mid))
                unread = 0 if row["is_read"] else 1
                c.execute("UPDATE folders SET total = MAX(total - 1, 0),"
                          " unread = MAX(unread - ?, 0) WHERE id=?", (unread, row["folder_id"]))
                c.execute("UPDATE folders SET total = total + 1, unread = unread + ? WHERE id=?",
                          (unread, dest))
                self._queue(c, mailbox_id, mid, "move", dest, row["folder_id"])
                changed.append(mid)
        return changed

    def delete_messages(self, mailbox_id: str, message_ids: list[str]) -> list[str]:
        """Delete messages for good: hidden at once (folder_id "", counts updated), removed
        once the server has done it, put back if it refuses."""
        changed = []
        with self.conn as c:
            for mid in message_ids:
                row = c.execute("SELECT folder_id, is_read FROM messages WHERE id=?",
                                (mid,)).fetchone()
                if row is None or not row["folder_id"]:
                    continue
                c.execute("UPDATE messages SET folder_id='' WHERE id=?", (mid,))
                c.execute("UPDATE folders SET total = MAX(total - 1, 0),"
                          " unread = MAX(unread - ?, 0) WHERE id=?",
                          (0 if row["is_read"] else 1, row["folder_id"]))
                self._queue(c, mailbox_id, mid, "delete", "1", row["folder_id"])
                changed.append(mid)
        return changed

    def clear_folders(self, folder_ids: list[str]) -> None:
        """Forget the messages of folders that were emptied on the server."""
        with self.conn as c:
            for fid in folder_ids:
                ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE folder_id=?",
                                               (fid,))]
                c.executemany("DELETE FROM bodies WHERE message_id=?", [(i,) for i in ids])
                c.executemany("DELETE FROM pending WHERE message_id=?", [(i,) for i in ids])
                c.execute("DELETE FROM messages WHERE folder_id=?", (fid,))
                c.execute("UPDATE folders SET total=0, unread=0 WHERE id=?", (fid,))

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
            row = c.execute("SELECT field, message_id FROM pending WHERE id=?",
                            (change_id,)).fetchone()
            c.execute("DELETE FROM pending WHERE id=?", (change_id,))
            if row is not None and row["field"] == "delete":  # gone on the server: forget it
                c.execute("DELETE FROM pending WHERE message_id=?", (row["message_id"],))
                c.execute("DELETE FROM bodies WHERE message_id=?", (row["message_id"],))
                c.execute("DELETE FROM messages WHERE id=?", (row["message_id"],))

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
            elif row["field"] in ("move", "delete"):
                c.execute("UPDATE messages SET folder_id=? WHERE id=?",
                          (row["old"], row["message_id"]))
            else:
                c.execute("UPDATE messages SET flag=? WHERE id=?", (row["old"], row["message_id"]))

    def oldest_received(self, folder_id: str) -> str | None:
        return self.conn.execute(
            "SELECT MIN(received) FROM messages WHERE folder_id=?", (folder_id,)).fetchone()[0]

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
            self._evict(c, message_id, max_bytes)

    @staticmethod
    def _evict(c, keep: str, max_bytes: int) -> None:
        """Drop the least recently read bodies (and their pictures) until under max_bytes."""
        total = c.execute("SELECT COALESCE(SUM(size),0) FROM bodies").fetchone()[0]
        while total > max_bytes:
            oldest = c.execute(
                "SELECT message_id, size FROM bodies WHERE message_id != ? "
                "ORDER BY last_access ASC LIMIT 1", (keep,)
            ).fetchone()
            if oldest is None:
                break
            c.execute("DELETE FROM bodies WHERE message_id=?", (oldest["message_id"],))
            total -= oldest["size"]

    def inline_images(self, message_id: str) -> dict[str, tuple[str, bytes]]:
        rows = self.conn.execute(
            "SELECT content_id, content_type, data FROM inline_images WHERE message_id=?",
            (message_id,)).fetchall()
        return {r["content_id"]: (r["content_type"], bytes(r["data"])) for r in rows}

    def put_inline_images(self, message_id: str, images: dict[str, tuple[str, bytes]],
                          max_bytes: int) -> None:
        """Store a cached body's pictures; they add to its size (only while it's cached)."""
        with self.conn as c:
            if c.execute("SELECT 1 FROM bodies WHERE message_id=?", (message_id,)).fetchone() \
                    is None:
                return
            added = 0
            for cid, (ctype, data) in images.items():
                cur = c.execute("INSERT OR IGNORE INTO inline_images"
                                "(message_id, content_id, content_type, data) VALUES(?,?,?,?)",
                                (message_id, cid, ctype, data))
                added += len(data) if cur.rowcount else 0
            c.execute("UPDATE bodies SET size = size + ? WHERE message_id=?", (added, message_id))
            self._evict(c, message_id, max_bytes)

    # -- contacts ----------------------------------------------------------
    _CONTACT_COLUMNS = ("id", "account_id", "source", "folder_id", "folder_name",
                        "display_name", "given_name", "surname", "company", "job_title",
                        "department", "office", "emails", "phones", "notes", "addresses",
                        "birthday", "website", "nickname")

    def replace_contacts(self, account_id: str, source: str, rows: list[dict]) -> None:
        """Make `rows` the account's whole set of contacts from `source`."""
        cols = self._CONTACT_COLUMNS
        with self.conn as c:
            c.execute("DELETE FROM contacts WHERE account_id=? AND source=?", (account_id, source))
            c.executemany(
                f"INSERT OR REPLACE INTO contacts({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})",
                [tuple(r.get(k) for k in cols) for r in rows])

    def upsert_contact(self, row: dict) -> None:
        cols = self._CONTACT_COLUMNS
        with self.conn as c:
            c.execute(f"INSERT OR REPLACE INTO contacts({','.join(cols)}) "
                      f"VALUES({','.join('?' * len(cols))})", tuple(row.get(k) for k in cols))

    def delete_contact(self, contact_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
            c.execute("DELETE FROM photos WHERE id=?", (contact_id,))

    # -- photos ------------------------------------------------------------
    def photo(self, contact_id: str) -> tuple[bytes | None, float] | None:
        """(image bytes or None for "has no photo", when asked), or None if never asked."""
        row = self.conn.execute("SELECT data, fetched FROM photos WHERE id=?",
                                (contact_id,)).fetchone()
        return (row["data"], row["fetched"]) if row else None

    def set_photo(self, contact_id: str, data: bytes | None) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO photos(id, data, fetched) VALUES(?,?,?)",
                      (contact_id, data, time.time()))

    def contact_row(self, contact_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()

    def contacts(self, source: str | None = None, folder_id: str | None = None,
                 search: str = "") -> list[sqlite3.Row]:
        """Contacts A-Z; every word of `search` must appear in the name, an address, the
        company, title or department."""
        sql, args = "SELECT * FROM contacts WHERE 1", []
        if source:
            sql += " AND source=?"
            args.append(source)
        if folder_id is not None:
            sql += " AND folder_id=?"
            args.append(folder_id)
        for word in search.split():
            sql += (" AND (display_name LIKE ? OR emails LIKE ? OR company LIKE ?"
                    " OR job_title LIKE ? OR department LIKE ?)")
            args.extend([f"%{word}%"] * 5)
        return self.conn.execute(sql + " ORDER BY display_name COLLATE NOCASE", args).fetchall()

    def contact_folders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT DISTINCT account_id, folder_id, folder_name FROM contacts "
            "WHERE source='personal' ORDER BY folder_id != '', folder_name COLLATE NOCASE"
        ).fetchall()

    def replace_groups(self, account_id: str, groups: list[dict]) -> None:
        with self.conn as c:
            c.execute("DELETE FROM contact_groups WHERE account_id=?", (account_id,))
            c.executemany(
                "INSERT OR REPLACE INTO contact_groups(id, account_id, name, notes, members)"
                " VALUES(?,?,?,?,?)",
                [(g["id"], account_id, g["name"], g.get("notes"), json.dumps(g["members"]))
                 for g in groups])

    def groups(self, search: str = "") -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM contact_groups WHERE 1", []
        for word in search.split():
            sql += " AND (name LIKE ? OR members LIKE ?)"
            args.extend([f"%{word}%"] * 2)
        return self.conn.execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()

    def upsert_group(self, account_id: str, group: dict) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO contact_groups(id, account_id, name, notes, members)"
                      " VALUES(?,?,?,?,?)", (group["id"], account_id, group["name"],
                                             group.get("notes"), json.dumps(group["members"])))

    def delete_group(self, group_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM contact_groups WHERE id=?", (group_id,))

    def group_row(self, group_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM contact_groups WHERE id=?",
                                 (group_id,)).fetchone()

    def rebuild_recipients(self) -> int:
        """Recount who you write to (To/Cc of mail in Sent Items) and reply to (senders of
        mail marked replied/replied-all). Returns how many people that is."""
        people: dict[str, dict] = {}

        def bump(name, address, field, when):
            if not address or "@" not in address:
                return
            p = people.setdefault(address.lower(), {"name": "", "sent": 0, "replied": 0,
                                                    "last_used": ""})
            p[field] += 1
            if name and (not p["name"] or (when or "") >= p["last_used"]):
                p["name"] = name
            p["last_used"] = max(p["last_used"], when or "")

        for r in self.conn.execute(
                "SELECT m.to_recipients, m.cc_recipients, m.received FROM messages m "
                "JOIN folders f ON f.id = m.folder_id WHERE f.well_known = 'sentitems'"):
            for name, address in recipients(r["to_recipients"]) + recipients(r["cc_recipients"]):
                bump(name, address, "sent", r["received"])
        for r in self.conn.execute(
                "SELECT sender_name, sender_addr, received FROM messages "
                "WHERE last_verb IN (102, 103)"):
            bump(r["sender_name"], r["sender_addr"], "replied", r["received"])
        mine = {(m["address"] or "").lower() for m in self.mailbox_rows()}
        mine |= {(m["display_name"] or "").lower() for m in self.mailbox_rows()}
        with self.conn as c:
            c.execute("DELETE FROM recipients")
            c.executemany(
                "INSERT INTO recipients(address, name, sent, replied, last_used) VALUES(?,?,?,?,?)",
                [(a, p["name"], p["sent"], p["replied"], p["last_used"])
                 for a, p in people.items() if a not in mine])
        return len(people)

    def recent_recipients(self, search: str = "", limit: int = 500) -> list[sqlite3.Row]:
        """Most used first: replies count double, recent use breaks ties."""
        sql, args = "SELECT * FROM recipients WHERE 1", []
        for word in search.split():
            sql += " AND (name LIKE ? OR address LIKE ?)"
            args.extend([f"%{word}%"] * 2)
        sql += " ORDER BY sent + 2 * replied DESC, last_used DESC LIMIT ?"
        return self.conn.execute(sql, (*args, limit)).fetchall()

    # -- calendar ----------------------------------------------------------
    def replace_calendars(self, account_id: str, rows: list[dict]) -> None:
        """The account's calendars, keeping which ones you chose to hide."""
        with self.conn as c:
            hidden = {r["id"] for r in c.execute(
                "SELECT id FROM calendars WHERE account_id=? AND visible=0", (account_id,))}
            gone = {r["id"] for r in c.execute(
                "SELECT id FROM calendars WHERE account_id=?", (account_id,))} - \
                {r["id"] for r in rows}
            for cid in gone:
                c.execute("DELETE FROM events WHERE calendar_id=?", (cid,))
            c.execute("DELETE FROM calendars WHERE account_id=?", (account_id,))
            c.executemany(
                "INSERT INTO calendars(id, account_id, name, color, is_default, can_edit, teams,"
                " owner, visible) VALUES(?,?,?,?,?,?,?,?,?)",
                [(r["id"], account_id, r["name"], r["color"], r["is_default"], r["can_edit"],
                  r["teams"], r["owner"], 0 if r["id"] in hidden else 1) for r in rows])

    def calendar_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM calendars ORDER BY is_default DESC, name COLLATE NOCASE").fetchall()

    def calendar_row(self, calendar_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM calendars WHERE id=?",
                                 (calendar_id,)).fetchone()

    def set_calendar_visible(self, calendar_id: str, visible: bool) -> None:
        with self.conn as c:
            c.execute("UPDATE calendars SET visible=? WHERE id=?", (int(visible), calendar_id))

    _EVENT_COLUMNS = ("id", "account_id", "calendar_id", "subject", "start", "end", "all_day",
                      "show_as", "categories", "location", "organizer_name", "organizer_addr",
                      "is_organizer", "attendees", "online_url", "preview", "response",
                      "cancelled", "kind", "series_id", "sensitivity", "reminder")

    def replace_events(self, calendar_id: str, start_utc: str, end_utc: str,
                       first_day: str, last_day: str, rows: list[dict]) -> None:
        """Make `rows` the calendar's events in a period: timed ones overlapping
        [start_utc, end_utc), all-day ones overlapping the days [first_day, last_day)."""
        cols = self._EVENT_COLUMNS
        with self.conn as c:
            c.execute("DELETE FROM events WHERE calendar_id=? AND ((all_day=0 AND start<? AND end>?)"
                      " OR (all_day=1 AND start<? AND end>?))",
                      (calendar_id, end_utc, start_utc, last_day, first_day))
            c.executemany(f"INSERT OR REPLACE INTO events({','.join(cols)}) "
                          f"VALUES({','.join('?' * len(cols))})",
                          [tuple(r.get(k) for k in cols) for r in rows])

    def upsert_event(self, row: dict) -> None:
        cols = self._EVENT_COLUMNS
        with self.conn as c:
            c.execute(f"INSERT OR REPLACE INTO events({','.join(cols)}) "
                      f"VALUES({','.join('?' * len(cols))})", tuple(row.get(k) for k in cols))

    def delete_event(self, event_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM events WHERE id=?", (event_id,))

    def event_row(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def events_between(self, start_utc: str, end_utc: str, first_day: str,
                       last_day: str) -> list[sqlite3.Row]:
        """Events in visible calendars overlapping a period (see replace_events)."""
        return self.conn.execute(
            "SELECT e.*, k.color AS calendar_color FROM events e "
            "JOIN calendars k ON k.id = e.calendar_id WHERE k.visible = 1 AND "
            "((e.all_day=0 AND e.start<? AND e.end>?) OR (e.all_day=1 AND e.start<? AND e.end>?))"
            " ORDER BY e.all_day DESC, e.start, e.subject COLLATE NOCASE",
            (end_utc, start_utc, last_day, first_day)).fetchall()

    def reminder_states(self) -> dict:
        return {(r["event_id"], r["start"]): (bool(r["dismissed"]), r["snoozed_until"])
                for r in self.conn.execute("SELECT * FROM reminder_state")}

    def set_reminder_state(self, event_id: str, start: str, dismissed: bool = False,
                           snoozed_until: str | None = None) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO reminder_state(event_id, start, dismissed,"
                      " snoozed_until) VALUES(?,?,?,?)",
                      (event_id, start, int(dismissed), snoozed_until))
            # forget states of events long gone
            c.execute("DELETE FROM reminder_state WHERE start < date('now', '-30 days')")

    def events_with_reminders(self, start_utc: str, end_utc: str, first_day: str,
                              last_day: str) -> list[sqlite3.Row]:
        """Events with a reminder in a period, from every calendar (hidden ones too)."""
        return self.conn.execute(
            "SELECT * FROM events WHERE reminder IS NOT NULL AND "
            "((all_day=0 AND start<? AND end>?) OR (all_day=1 AND start<? AND end>?))",
            (end_utc, start_utc, last_day, first_day)).fetchall()

    def replace_categories(self, account_id: str, rows: list[tuple[str, str]]) -> None:
        with self.conn as c:
            c.execute("DELETE FROM categories WHERE account_id=?", (account_id,))
            c.executemany("INSERT OR REPLACE INTO categories(account_id, name, color)"
                          " VALUES(?,?,?)", [(account_id, n, col) for n, col in rows])

    def category_colors(self) -> dict[str, str]:
        return {r["name"]: r["color"] for r in self.conn.execute(
            "SELECT name, color FROM categories ORDER BY name COLLATE NOCASE")}

    # -- signatures -------------------------------------------------------------
    def signature(self, account_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM signatures WHERE account_id=?",
                                 (account_id,)).fetchone()

    def set_signature(self, account_id: str, html: str, on_new: bool, on_reply: bool) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO signatures(account_id, html, on_new, on_reply)"
                      " VALUES(?,?,?,?)", (account_id, html, int(on_new), int(on_reply)))

    def has_body(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM bodies WHERE message_id=?", (message_id,)).fetchone() is not None

    def pending_count(self) -> int:
        """Local changes and messages not yet sent, all mailboxes."""
        return self.conn.execute("SELECT (SELECT COUNT(*) FROM pending)"
                                 " + (SELECT COUNT(*) FROM outbox)").fetchone()[0]

    # -- tasks -----------------------------------------------------------
    _TASK_COLUMNS = ("id", "account_id", "list_id", "title", "status", "importance", "due",
                     "start", "reminder", "completed", "body", "body_type", "categories",
                     "created", "modified", "recurrence")

    def replace_task_lists(self, account_id: str, rows: list[dict]) -> None:
        """Make `rows` the account's lists; tasks of lists that are gone go too."""
        keep = [r["id"] for r in rows]
        with self.conn as c:
            gone = [r[0] for r in c.execute("SELECT id FROM task_lists WHERE account_id=?",
                                            (account_id,)) if r[0] not in keep]
            for lid in gone:
                c.execute("DELETE FROM tasks WHERE list_id=?", (lid,))
                c.execute("DELETE FROM task_lists WHERE id=?", (lid,))
                c.execute("DELETE FROM sync_state WHERE key=?", (f"todo:{lid}",))
            c.executemany("INSERT OR REPLACE INTO task_lists(id, account_id, name, well_known,"
                          " shared) VALUES(:id, :account_id, :name, :well_known, :shared)", rows)

    def task_lists(self, account_id: str | None = None) -> list[sqlite3.Row]:
        """Tasks first, Flagged email next, then the rest by name."""
        sql, args = "SELECT * FROM task_lists", ()
        if account_id is not None:
            sql, args = sql + " WHERE account_id=?", (account_id,)
        return self.conn.execute(
            sql + " ORDER BY account_id, CASE well_known WHEN 'defaultList' THEN 0"
            " WHEN 'flaggedEmails' THEN 1 ELSE 2 END, name COLLATE NOCASE", args).fetchall()

    def task_list(self, list_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM task_lists WHERE id=?", (list_id,)).fetchone()

    def upsert_task(self, row: dict) -> None:
        """Insert or update a task; its links stay unless `row` has them."""
        cols = self._TASK_COLUMNS + (("links",) if "links" in row else ())
        with self.conn as c:
            c.execute(f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
                      f" ON CONFLICT(id) DO UPDATE SET "
                      + ", ".join(f"{k}=excluded.{k}" for k in cols if k != "id"),
                      tuple(row.get(k) for k in cols))

    def apply_task_changes(self, rows: list[dict], removed: list[str]) -> None:
        for row in rows:
            self.upsert_task(row)
        with self.conn as c:
            c.executemany("DELETE FROM tasks WHERE id=?", [(i,) for i in removed])

    def clear_task_list(self, list_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM tasks WHERE list_id=?", (list_id,))

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks(self, list_ids: list[str] | None = None, completed: bool = True,
              search: str = "") -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM tasks WHERE 1", []
        if list_ids is not None:
            sql += f" AND list_id IN ({','.join('?' * len(list_ids))})"
            args += list_ids
        if not completed:
            sql += " AND status != 'completed'"
        for word in search.split():
            sql += " AND (title LIKE ? OR body LIKE ? OR categories LIKE ?)"
            args += [f"%{word}%"] * 3
        return self.conn.execute(sql + " ORDER BY due IS NULL, due, importance = 'high' DESC,"
                                 " title COLLATE NOCASE", args).fetchall()

    def tasks_with_reminders(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks WHERE reminder IS NOT NULL"
                                 " AND status != 'completed'").fetchall()

    def task_counts(self, today: str) -> dict[str, tuple[int, int]]:
        """Per list: (open tasks, overdue ones)."""
        return {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT list_id, COUNT(*), SUM(due IS NOT NULL AND due < ?) FROM tasks"
            " WHERE status != 'completed' GROUP BY list_id", (today,))}

    def update_task(self, task_id: str, **fields) -> None:
        """Change some of a task's columns here (before the server has it)."""
        cols = [k for k in fields if k in self._TASK_COLUMNS and k != "id"]
        with self.conn as c:
            c.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                      (*[fields[k] for k in cols], task_id))

    def set_task_links(self, task_id: str, links: list) -> None:
        with self.conn as c:
            c.execute("UPDATE tasks SET links=? WHERE id=?", (json.dumps(links), task_id))

    def delete_task(self, task_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    # -- the Outbox ------------------------------------------------------
    def outbox_add(self, mailbox_id: str, payload: dict, error: str | None = None) -> int:
        with self.conn as c:
            cur = c.execute("INSERT INTO outbox(mailbox_id, created, payload, error)"
                            " VALUES(?,?,?,?)",
                            (mailbox_id, dt.datetime.now(dt.timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"), json.dumps(payload), error))
        return cur.lastrowid

    def outbox_items(self, mailbox_id: str | None = None,
                     refused: bool = True) -> list[sqlite3.Row]:
        """Oldest first; without `refused`, only the ones to try again by themselves."""
        sql, args = "SELECT * FROM outbox WHERE 1", []
        if mailbox_id is not None:
            sql, args = sql + " AND mailbox_id=?", [mailbox_id]
        if not refused:
            sql += " AND refused=0"
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def outbox_item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM outbox WHERE id=?", (item_id,)).fetchone()

    def outbox_update(self, item_id: int, payload: dict, error: str | None,
                      refused: bool = False) -> None:
        with self.conn as c:
            c.execute("UPDATE outbox SET payload=?, error=?, refused=? WHERE id=?",
                      (json.dumps(payload), error, int(refused), item_id))

    def outbox_remove(self, item_id: int) -> None:
        with self.conn as c:
            c.execute("DELETE FROM outbox WHERE id=?", (item_id,))

    def outbox_counts(self) -> dict[str, int]:
        """Waiting messages per mailbox."""
        return dict(self.conn.execute(
            "SELECT mailbox_id, COUNT(*) FROM outbox GROUP BY mailbox_id").fetchall())

    def outbox_rows(self, mailbox_id: str, folder_id: str, me: str = "") -> list[dict]:
        """The waiting messages of a mailbox as message-list rows (ids "outbox:<n>")."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(messages)")]
        out = []
        for item in self.outbox_items(mailbox_id):
            p = json.loads(item["payload"])
            row = dict.fromkeys(cols)
            text = body_text("html" if p["html"] else "text", p["body"])
            note = (f"Not sent: {item['error']}" if item["refused"] else
                    "Waiting to be sent" + (f" ({item['error']})" if item["error"] else ""))
            row.update(id=f"outbox:{item['id']}", folder_id=folder_id, subject=p["subject"],
                       sender_name=me, sender_addr=me, received=item["created"], is_read=1,
                       has_attachments=int(bool(p["attachments"])),
                       preview=f"{note} · {text[:200]}",
                       to_recipients=json.dumps(_people(p["to"])),
                       cc_recipients=json.dumps(_people(p["cc"])),
                       importance="normal", flag="notFlagged",
                       size=len(p["body"]) + sum(a["size"] for a in p["attachments"]))
            out.append(row)
        return out

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
