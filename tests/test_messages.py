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


def test_new_messages_are_reported_only_on_incremental_sync(tmp_path):
    st = make_store(tmp_path, msg("m1"))

    class G:
        def __init__(self, pages):
            self.pages = pages

        def messages_delta(self, folder_id, link, since):
            yield from self.pages

    eng = SyncEngine(st, G([([msg("m2"), msg("m3")], "L1")]), "a")
    assert eng.sync_folder("F").new_ids == []  # first sync: everything is "new"
    eng.graph = G([([msg("m3", subject="edited"), msg("m4")], "L2")])
    assert eng.sync_folder("F").new_ids == ["m4"]  # m3 was only updated


class PagesGraph:
    """messages_delta replays fixed pages and records the `since` it was asked for."""

    def __init__(self, pages, older=()):
        self.pages, self.older, self.calls = pages, list(older), []

    def messages_delta(self, folder_id, link, since):
        self.calls.append((link, since))
        yield from self.pages

    def messages_before(self, folder_id, before, limit):
        self.before = before
        return [m for m in self.older if m["receivedDateTime"] < before][:limit]


def test_longer_sync_period_refetches(tmp_path):
    st = make_store(tmp_path)
    g = PagesGraph([([msg("m1")], "L1")])
    eng = SyncEngine(st, g, "a", initial_days=90)
    eng.sync_folder("F")
    eng.sync_folder("F")
    assert [link for link, _ in g.calls] == [None, "L1"]   # incremental the second time
    eng.initial_days = 180
    eng.sync_folder("F")
    assert g.calls[-1][0] is None                           # period grew: start over
    assert g.calls[-1][1] < g.calls[0][1]                   # ... reaching further back
    eng.initial_days = 30
    eng.sync_folder("F")
    assert g.calls[-1][0] == "L1"                           # shorter: keep the cursor


def test_cursor_without_recorded_period_is_redone_once(tmp_path):
    st = make_store(tmp_path)
    st.set_state("delta:F", "OLD")  # made by an earlier version
    g = PagesGraph([([msg("m1")], "L1")])
    eng = SyncEngine(st, g, "a", initial_days=180)
    eng.sync_folder("F")
    eng.sync_folder("F")
    assert [link for link, _ in g.calls] == [None, "L1"]


def test_older_mail_on_request_survives_a_resync(tmp_path):
    t = "receivedDateTime"
    st = make_store(tmp_path, msg("m5", **{t: "2026-11-10T10:00:00Z"}))
    old = [msg("o1", **{t: "2020-05-01T00:00:00Z"}), msg("o2", **{t: "2019-01-01T00:00:00Z"})]
    g = PagesGraph([([msg("m5", **{t: "2026-11-10T10:00:00Z"})], "L1")], older=old)
    eng = SyncEngine(st, g, "a", initial_days=30)
    assert eng.fetch_older("F") == 2 and g.before == "2026-11-10T10:00:00Z"
    st.delete_state("delta:F")
    eng.sync_folder("F")  # full resync of the last 30 days: o1/o2 aren't in it
    assert {r["id"] for r in st.list_messages("F")} == {"m5", "o1", "o2"}


def test_download_period_covers_what_the_list_shows(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from waddlemail.settings import Settings
    QApplication.instance() or QApplication([])  # (other tests need a full QApplication)
    s = Settings(str(tmp_path / "s.ini"))
    for keep, show, expected in ((180, 0, 0), (180, 30, 180), (90, 365, 365), (0, 30, 0)):
        s.set("sync/window_days", keep)
        s.set("list/window_days", show)
        assert s.download_days() == expected, (keep, show)


def test_list_has_no_size_cap(tmp_path):
    st = make_store(tmp_path)
    st.apply_message_changes("F", [{"id": f"x{i}", "receivedDateTime": "2024-01-01T00:00:00Z"}
                                   for i in range(5200)])
    assert len(st.list_messages("F")) == 5200


def test_delta_asks_for_reply_state_with_a_plain_eq_filter():
    """Exchange accepts only "id eq" in this filter; an "or" made it refuse the request."""
    from tests.test_mailboxes import FakeResponse

    class Session:
        def request(self, method, url, params=None, **kw):
            self.params = dict(params or {})
            return FakeResponse(200, {"value": [], "@odata.deltaLink": "L"})

    s = Session()
    list(GraphClient(lambda: "t", session=s).messages_delta("F"))
    assert s.params["$expand"] == ("singleValueExtendedProperties("
                                   "$filter=id eq 'Integer 0x1081')")


def test_cursors_are_remade_once_when_the_query_changes(tmp_path):
    st = make_store(tmp_path, msg("m1"))
    st.set_state("delta:F", "OLD-QUERY")
    st.set_state("delta_query", "1")  # made by the version with the refused "or" filter
    st.conn.commit()
    st2 = Store(tmp_path / "m.db")
    assert st2.get_state("delta:F") is None and st2.get_state("delta_query") == "2"
    assert st2.message_row("m1") is not None  # mail stays; only the cursor goes
    st2.set_state("delta:F", "NEW")
    assert Store(tmp_path / "m.db").get_state("delta:F") == "NEW"  # only once


def test_marking_responded_locally(tmp_path):
    st = make_store(tmp_path, msg("m1"))
    st.mark_responded("m1", 103)
    assert st.message_row("m1")["last_verb"] == 103
    st.apply_message_changes("F", [{"id": "m1", "subject": "later update"}])
    assert st.message_row("m1")["last_verb"] == 103  # kept until the server says otherwise


def test_message_sizes_request():
    from tests.test_mailboxes import FakeResponse

    class Session:
        def request(self, method, url, params=None, **kw):
            self.params = dict(params or {})
            return FakeResponse(200, {"value": [
                {"id": "a", "singleValueExtendedProperties": [
                    {"id": "Integer 0xe08", "value": "2048"}]},
                {"id": "b"}]})

    s = Session()
    [page] = list(GraphClient(lambda: "t", session=s).message_sizes("F"))
    assert page == {"a": 2048}
    assert s.params["$expand"] == "singleValueExtendedProperties($filter=id eq 'Integer 0x0E08')"
    assert s.params["$select"] == "id" and s.params["$top"] == 500


def test_fill_sizes(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"), msg("m3"))

    class G:
        def __init__(self, pages=(), status=None):
            self.pages, self.status, self.asked = list(pages), status, 0

        def message_sizes(self, folder_id):
            self.asked += 1
            if self.status:
                raise GraphError("no", self.status)
            yield from self.pages

    g = G([{"m3": 300, "x": 1}, {"m2": 200}, {"m1": 100}])
    eng = SyncEngine(st, g, "a")
    assert eng.fill_sizes("F", max_pages=2) == 2          # two pages, then stops for now
    assert st.missing_sizes("F") == {"m1"}
    assert eng.fill_sizes("F") == 1 and not st.missing_sizes("F")
    g.asked = 0
    assert eng.fill_sizes("F") == 0 and g.asked == 0      # all known: no request
    st.apply_message_changes("F", [{"id": "m1", "subject": "changed"}])
    assert st.message_row("m1")["size"] == 100            # a later update keeps it
    st.apply_message_changes("F", [msg("m4")])
    refusing = SyncEngine(st, G(status=400), "a")
    assert refusing.fill_sizes("F") == 0 and refusing.sizes_refused
