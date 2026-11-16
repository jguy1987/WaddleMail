"""Deleting messages for good (through the change queue) and emptying Deleted Items."""
from __future__ import annotations

from waddlemail.graph import GraphClient, GraphError
from waddlemail.sync import SyncEngine
from tests.test_mailboxes import FakeResponse
from tests.test_messages import make_store, msg


class DeleteGraph:
    def __init__(self, status=None):
        self.deleted, self.status = [], status

    def delete_message(self, mid):
        if self.status:
            raise GraphError("nope", self.status)
        self.deleted.append(mid)


def counts(st):
    row = st.folder_row("F")
    return row["total"], row["unread"]


def test_delete_hides_then_forgets(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2", isRead=True))
    st.conn.execute("UPDATE folders SET total=2, unread=1 WHERE id='F'")
    st.put_body("m1", "text", "hello", 10 ** 6)
    assert st.delete_messages("a", ["m1", "m2", "nope"]) == ["m1", "m2"]
    assert st.message_row("m1")["folder_id"] == "" and counts(st) == (0, 0)
    assert st.delete_messages("a", ["m1"]) == []  # (already on its way)
    g = DeleteGraph()
    assert SyncEngine(st, g, "a").send_changes() == 2
    assert g.deleted == ["m1", "m2"] and st.message_row("m1") is None
    assert st.get_body("m1") is None and st.pending_changes("a") == []


def test_delete_refused_puts_it_back_but_gone_is_done(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    st.delete_messages("a", ["m1"])
    SyncEngine(st, DeleteGraph(403), "a").send_changes()  # read-only shared mailbox
    assert st.message_row("m1")["folder_id"] == "F" and st.pending_changes("a") == []
    st.delete_messages("a", ["m2"])
    SyncEngine(st, DeleteGraph(404), "a").send_changes()  # deleted elsewhere already
    assert st.message_row("m2") is None and st.pending_changes("a") == []


def test_clear_folders(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    st.put_body("m1", "text", "hello", 10 ** 6)
    st.set_read("a", ["m2"], True)
    st.clear_folders(["F"])
    assert st.message_row("m1") is None and st.get_body("m1") is None
    assert st.pending_changes("a") == [] and counts(st) == (0, 0)


def test_batch_delete_retries_throttled_and_reports_refused():
    calls = []

    class S:
        def request(self, method, url, json=None, **kw):
            if "/mailFolders/" in url:
                return FakeResponse(200, {"value": [{"id": f"m{i}"} for i in range(25)]})
            calls.append([r["url"].rsplit("/", 1)[1] for r in json["requests"]])
            out = []
            for r in json["requests"]:
                mid = r["url"].rsplit("/", 1)[1]
                status = 204
                if mid == "m3" and len(calls) == 1:
                    status = 429      # throttled the first time
                elif mid == "m5":
                    status = 403      # not allowed
                elif mid == "m7":
                    status = 404      # gone already: fine
                out.append({"id": r["id"], "status": status, "headers": {"Retry-After": "0"}})
            return FakeResponse(200, {"responses": out})

    g = GraphClient(lambda: "t", session=S(), sleep=lambda s: None)
    ids = g.folder_message_ids("T")
    seen = []
    refused = g.delete_messages(ids, lambda done, total: seen.append((done, total)))
    assert refused == ["m5"]
    assert [len(c) for c in calls] == [20, 1, 5] and calls[1] == ["m3"]
    assert seen == [(20, 25), (25, 25)]
