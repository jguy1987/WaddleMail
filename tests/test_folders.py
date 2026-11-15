"""Folder create / rename / move / delete requests, and the store's folder tree helpers."""
from __future__ import annotations

from waddlemail.graph import GraphClient
from waddlemail.store import Store
from tests.test_mailboxes import FakeResponse


class Recorder:
    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append((method, url.removeprefix("https://graph.microsoft.com/v1.0"), json))
        if method == "DELETE":
            return FakeResponse(204, "")
        return FakeResponse(200, {"id": "NEW", "displayName": (json or {}).get("displayName")})


def test_folder_requests():
    s = Recorder()
    g = GraphClient(lambda: "t", session=s, user="box@x.com")
    assert g.create_folder("Top")["id"] == "NEW"
    g.create_folder("Sub", "P/1")
    g.rename_folder("F", "Renamed")
    g.move_folder("F", "deleteditems")
    g.delete_folder("F")
    root = "/users/box@x.com/mailFolders"
    assert s.calls == [
        ("POST", root, {"displayName": "Top"}),
        ("POST", f"{root}/P%2F1/childFolders", {"displayName": "Sub"}),
        ("PATCH", f"{root}/F", {"displayName": "Renamed"}),
        ("POST", f"{root}/F/move", {"destinationId": "deleteditems"}),
        ("DELETE", f"{root}/F", None),
    ]


def test_folder_path_and_subtree(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    f = lambda i, p=None, **kw: {"id": i, "displayName": i, "parentFolderId": p, **kw}  # noqa: E731
    st.upsert_folders("a", [f("del", wellKnownName="deleteditems"), f("x", "del"),
                            f("y", "x"), f("z", "y", totalItemCount=4), f("other")])
    assert [r["id"] for r in st.folder_path("z")] == ["del", "x", "y", "z"]
    assert {r["id"] for r in st.folder_subtree("x")} == {"x", "y", "z"}
    assert {r["id"] for r in st.folder_subtree("other")} == {"other"}


def test_message_move_and_copy_requests():
    s = Recorder()
    g = GraphClient(lambda: "t", session=s)
    g.move_message("M", "F2")
    g.copy_message("M", "F2")
    assert s.calls == [("POST", "/me/messages/M/move", {"destinationId": "F2"}),
                       ("POST", "/me/messages/M/copy", {"destinationId": "F2"})]


def mail_store(tmp_path):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.upsert_folders("a", [{"id": "A", "displayName": "A", "unreadItemCount": 1,
                             "totalItemCount": 2},
                            {"id": "B", "displayName": "B"}])
    st.apply_message_changes("A", [{"id": "m1", "isRead": False},
                                   {"id": "m2", "isRead": True}])
    return st


def test_local_move_updates_counts_and_queues(tmp_path):
    st = mail_store(tmp_path)
    assert st.move_messages("a", ["m1", "m2", "missing"], "B") == ["m1", "m2"]
    a, b = st.folder_row("A"), st.folder_row("B")
    assert (a["total"], a["unread"], b["total"], b["unread"]) == (0, 0, 2, 1)
    assert [r["id"] for r in st.list_messages("B")] == ["m1", "m2"]
    assert [(c["field"], c["value"], c["old"]) for c in st.pending_changes("a")] == [
        ("move", "B", "A"), ("move", "B", "A")]
    # a stale page from A's delta (sent before the server moved it) must not pull it back
    st.apply_message_changes("A", [{"id": "m1", "parentFolderId": "A", "subject": "x"}])
    assert st.message_row("m1")["folder_id"] == "B"
    # nor may a full resync of B (the server doesn't have it there yet) drop it
    st.prune_folder("B", set())
    assert st.message_row("m1") is not None
    # moving back before it was sent cancels the change
    st.move_messages("a", ["m2"], "A")
    assert [c["message_id"] for c in st.pending_changes("a")] == ["m1"]


def test_move_is_sent_or_undone(tmp_path):
    from waddlemail.graph import GraphError
    from waddlemail.sync import SyncEngine

    class G:
        def __init__(self, status=None):
            self.status, self.moved = status, []

        def move_message(self, mid, dest):
            if self.status:
                raise GraphError("no", self.status)
            self.moved.append((mid, dest))

    st = mail_store(tmp_path)
    st.move_messages("a", ["m1"], "B")
    g = G()
    SyncEngine(st, g, "a").send_changes()
    assert g.moved == [("m1", "B")] and st.pending_changes("a") == []

    st.move_messages("a", ["m2"], "B")
    SyncEngine(st, G(403), "a").send_changes()  # e.g. read-only shared mailbox
    assert st.message_row("m2")["folder_id"] == "A"


def test_older_messages_request():
    class Session:
        def request(self, method, url, params=None, headers=None, json=None, timeout=None):
            self.params = params
            return FakeResponse(200, {"value": [{"id": str(i)} for i in range(100)],
                                      "@odata.nextLink": "https://graph.microsoft.com/v1.0/n"})

    s = Session()
    out = GraphClient(lambda: "t", session=s).messages_before("F", "2026-01-01T00:00:00Z", 150)
    assert len(out) == 150  # stops at the limit, across pages
    first = GraphClient(lambda: "t", session=Session())
    sess = first._s
    first.messages_before("F", "2026-01-01T00:00:00Z", 10)
    assert sess.params["$filter"] == "receivedDateTime lt 2026-01-01T00:00:00Z"
    assert sess.params["$orderby"] == "receivedDateTime desc"
