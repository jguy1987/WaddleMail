"""Mailbox-scoped folders, well-known folder discovery and shared-folder fallback (no network)."""
from __future__ import annotations

import pytest

from waddlemail.graph import GraphClient, GraphError
from waddlemail.store import Store


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.headers: dict = {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Routes (method, path) to canned responses; records every call."""

    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def request(self, method, url, params=None, headers=None, json=None, timeout=None):
        path = url.removeprefix("https://graph.microsoft.com/v1.0")
        self.calls.append((method, path, json))
        status, body = self.routes.get((method, path), (404, {"error": "not found"}))
        return FakeResponse(status, body)


def folder(fid, name, parent="root", children=0, unread=0):
    return {"id": fid, "displayName": name, "parentFolderId": parent,
            "childFolderCount": children, "unreadItemCount": unread, "totalItemCount": unread}


def batch(**found):
    return (200, {"responses": [{"id": k, "status": 200, "body": v} for k, v in found.items()]
                  + [{"id": "archive", "status": 404, "body": {}}]})


def test_full_mailbox_walk_tags_well_known():
    inbox, sub = folder("I", "Inbox", children=1), folder("S", "Projects", parent="I")
    s = FakeSession({
        ("POST", "/$batch"): batch(inbox=inbox),
        ("GET", "/users/shared@x.com/mailFolders"): (200, {"value": [inbox]}),
        ("GET", "/users/shared@x.com/mailFolders/I/childFolders"): (200, {"value": [sub]}),
    })
    g = GraphClient(lambda: "t", session=s, user="shared@x.com")
    out = g.list_folders()
    assert [f["id"] for f in out] == ["I", "S"]
    assert out[0]["wellKnownName"] == "inbox" and "wellKnownName" not in out[1]
    assert s.calls[0][2]["requests"][0]["url"].startswith("/users/shared@x.com/mailFolders/inbox")


def test_shared_folders_only_falls_back_to_well_known():
    inbox = folder("I", "Inbox")
    s = FakeSession({
        ("POST", "/$batch"): batch(inbox=inbox),
        ("GET", "/users/boss@x.com/mailFolders"): (403, {"error": "denied"}),
    })
    out = GraphClient(lambda: "t", session=s, user="boss@x.com").list_folders()
    assert [f["id"] for f in out] == ["I"]


def test_no_access_at_all_raises():
    s = FakeSession({("POST", "/$batch"): (200, {"responses": []}),
                     ("GET", "/users/nope@x.com/mailFolders"): (403, {})})
    with pytest.raises(GraphError):
        GraphClient(lambda: "t", session=s, user="nope@x.com").list_folders()


def test_own_mailbox_uses_me():
    s = FakeSession({("POST", "/$batch"): batch(), ("GET", "/me/mailFolders"): (200, {"value": []})})
    GraphClient(lambda: "t", session=s).list_folders()
    assert ("GET", "/me/mailFolders", None) in s.calls


def test_folder_sync_is_scoped_per_mailbox(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.add_mailbox("a/shared@x.com", "a", "shared@x.com", "Support")
    st.upsert_folders("a", [dict(folder("A1", "Inbox"), wellKnownName="inbox")])
    st.upsert_folders("a/shared@x.com", [folder("B1", "Inbox")])
    st.upsert_folders("a", [])  # A's folders gone; B's must survive
    assert [r["id"] for r in st.folder_rows()] == ["B1"]
    assert st.folder_mailbox("B1") == "a/shared@x.com"


def test_remove_mailbox_drops_its_mail(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.upsert_folders("a", [dict(folder("A1", "Inbox"), wellKnownName="inbox")])
    assert st.well_known_folder("a", "inbox") == "A1"
    st.apply_message_changes("A1", [{"id": "m1", "subject": "hi"}])
    st.set_state("delta:A1", "link")
    st.remove_mailbox("a")
    assert st.mailbox_rows() == [] and st.message_count("A1") == 0
    assert st.get_state("delta:A1") is None


def test_old_database_is_migrated_and_adopted(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE folders(id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL,"
              " child_count INTEGER DEFAULT 0, unread INTEGER DEFAULT 0, total INTEGER DEFAULT 0,"
              " collapsed INTEGER DEFAULT 0)")
    c.execute("INSERT INTO folders(id, name) VALUES('F', 'Inbox')")
    c.commit()
    c.close()
    st = Store(path)
    st.add_mailbox("acct", "acct", None, "me@x.com")
    st.adopt_orphan_folders("acct")
    assert st.folder_mailbox("F") == "acct"


def test_folder_order_puts_defaults_first():
    from waddlemail.ui.folder_pane import folder_order
    rows = [{"name": n, "well_known": w} for n, w in [
        ("Archive", "archive"), ("aardvark", None), ("Sent Items", "sentitems"),
        ("Inbox", "inbox"), ("Zebra", None), ("Drafts", "drafts")]]
    assert [r["name"] for r in folder_order(rows)] == [
        "Inbox", "Drafts", "Sent Items", "Archive", "aardvark", "Zebra"]
