"""Message fields, filtered listing, and queued read/flag changes (no network)."""
from __future__ import annotations

from waddlemail.graph import GraphClient, GraphError
from waddlemail.store import Store, recipients
from waddlemail.sync import SyncEngine


def msg(mid, **kw):
    item = {"id": mid, "subject": f"subject {mid}", "isRead": False,
            "from": {"emailAddress": {"name": "Pat Doe", "address": "pat@x.com"}},
            "receivedDateTime": f"2026-11-{10 + int(mid[-1]):02d}T10:00:00Z"}
    item.update(kw)
    return item


def make_store(tmp_path, *items) -> Store:
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.upsert_folders("a", [{"id": "F", "displayName": "Inbox", "wellKnownName": "inbox",
                             "unreadItemCount": len(items)}])
    st.apply_message_changes("F", list(items))
    return st


def test_new_fields_are_stored(tmp_path):
    st = make_store(tmp_path, msg("m1", importance="high", flag={"flagStatus": "flagged"},
                                  toRecipients=[{"emailAddress": {"name": "Sam",
                                                                  "address": "sam@x.com"}}],
                                  singleValueExtendedProperties=[
                                      {"id": "Integer 0x1081", "value": "102"},
                                      {"id": "Integer 0xe08", "value": "2048"}]))
    r = st.message_row("m1")
    assert (r["importance"], r["flag"], r["last_verb"], r["size"]) == ("high", "flagged", 102, 2048)
    assert recipients(r["to_recipients"]) == [("Sam", "sam@x.com")]


def test_partial_update_keeps_fields(tmp_path):
    st = make_store(tmp_path, msg("m1", importance="high", flag={"flagStatus": "flagged"}))
    st.apply_message_changes("F", [{"id": "m1", "isRead": True}])
    r = st.message_row("m1")
    assert (r["importance"], r["flag"], r["is_read"]) == ("high", "flagged", 1)


def test_search_matches_every_word_across_fields(tmp_path):
    st = make_store(tmp_path, msg("m1", subject="Quarterly report"),
                    msg("m2", subject="Lunch", bodyPreview="the report is late"),
                    msg("m3", subject="100% off_sale"))
    ids = lambda **kw: {r["id"] for r in st.list_messages("F", **kw)}  # noqa: E731
    assert ids(search="report") == {"m1", "m2"}
    assert ids(search="report pat") == {"m1", "m2"}       # sender matches "pat"
    assert ids(search="quarterly late") == set()          # each word must match
    assert ids(search="100%") == {"m3"} and ids(search="f_s") == {"m3"}
    assert ids(search="o%f") == set()                     # % and _ are literal


def test_filter_toggles(tmp_path):
    st = make_store(tmp_path, msg("m1", isRead=True, hasAttachments=True),
                    msg("m2", flag={"flagStatus": "flagged"}))
    ids = lambda **kw: {r["id"] for r in st.list_messages("F", **kw)}  # noqa: E731
    assert ids(unread=True) == {"m2"}
    assert ids(flagged=True) == {"m2"}
    assert ids(attachments=True) == {"m1"}


def test_mark_read_updates_counts_and_survives_stale_delta(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    assert st.set_read("a", ["m1", "m1"], True) == ["m1"]
    assert st.folder_row("F")["unread"] == 1
    # A delta page fetched before the change was sent still says unread: local change wins.
    st.apply_message_changes("F", [msg("m1", isRead=False)])
    assert st.message_row("m1")["is_read"] == 1
    assert [c["value"] for c in st.pending_changes("a")] == ["1"]


def test_toggling_back_before_sending_cancels(tmp_path):
    st = make_store(tmp_path, msg("m1"))
    st.set_read("a", ["m1"], True)
    st.set_read("a", ["m1"], False)
    assert st.pending_changes("a") == []


class FakeGraph:
    def __init__(self, fail_status=None):
        self.sent, self.fail_status = [], fail_status

    def update_message(self, mid, changes):
        if self.fail_status:
            raise GraphError("nope", self.fail_status)
        self.sent.append((mid, changes))


def test_send_changes(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    st.set_read("a", ["m1"], True)
    st.set_flag("a", ["m2"], "flagged")
    g = FakeGraph()
    assert SyncEngine(st, g, "a").send_changes() == 2
    assert g.sent == [("m1", {"isRead": True}), ("m2", {"flag": {"flagStatus": "flagged"}})]
    assert st.pending_changes("a") == []


def test_refused_change_is_undone_but_server_error_is_kept(tmp_path):
    st = make_store(tmp_path, msg("m1"))
    st.set_read("a", ["m1"], True)
    try:
        SyncEngine(st, FakeGraph(503), "a").send_changes()
    except GraphError:
        pass
    assert len(st.pending_changes("a")) == 1           # kept for the next try
    SyncEngine(st, FakeGraph(403), "a").send_changes()  # e.g. read-only shared mailbox
    assert st.pending_changes("a") == [] and st.message_row("m1")["is_read"] == 0


def test_delta_falls_back_when_expand_is_refused():
    from tests.test_mailboxes import FakeResponse

    class Session:
        def __init__(self):
            self.params = []

        def request(self, method, url, params=None, **kw):
            self.params.append(dict(params or {}))
            if params and "$expand" in params:
                return FakeResponse(400, {"error": "bad expand"})
            return FakeResponse(200, {"value": [], "@odata.deltaLink": "L"})

    s = Session()
    pages = list(GraphClient(lambda: "t", session=s).messages_delta("F"))
    assert pages == [([], "L")]
    assert "$expand" in s.params[0] and "$expand" not in s.params[1]


def test_one_stuck_change_does_not_block_the_rest(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    st.set_read("a", ["m1", "m2"], True)

    class G:
        sent = []

        def update_message(self, mid, changes):
            if mid == "m1":
                raise GraphError("busy", 503)
            self.sent.append(mid)

    try:
        SyncEngine(st, G(), "a").send_changes()
    except GraphError:
        pass
    assert G.sent == ["m2"]
    assert [c["message_id"] for c in st.pending_changes("a")] == ["m1"]
