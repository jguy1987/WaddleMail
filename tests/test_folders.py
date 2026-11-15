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
