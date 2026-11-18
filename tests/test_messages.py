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



def test_delta_doesnt_ask_for_extended_properties():
    """Exchange ignores $expand in a delta query, so it isn't sent (see graph.py)."""
    from tests.test_mailboxes import FakeResponse

    class Session:
        def request(self, method, url, params=None, **kw):
            self.params = dict(params or {})
            return FakeResponse(200, {"value": [], "@odata.deltaLink": "L"})

    s = Session()
    assert list(GraphClient(lambda: "t", session=s).messages_delta("F")) == [([], "L")]
    assert "$expand" not in s.params


def test_reply_mark_requests():
    from tests.test_mailboxes import FakeResponse

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, json=None, **kw):
            self.calls.append((method, url.split("v1.0")[1], params, json))
            if method == "POST":  # the $batch: m1 replied, m2 nothing, m3 gone
                return FakeResponse(200, {"responses": [
                    {"id": "0", "status": 200, "body": {"id": "m1",
                     "singleValueExtendedProperties": [{"id": "Integer 0x1081",
                                                        "value": "102"}]}},
                    {"id": "1", "status": 200, "body": {"id": "m2"}},
                    {"id": "2", "status": 404, "body": {}}]})
            return FakeResponse(200, {"value": [{"id": "m9"}]})

    s = Session()
    g = GraphClient(lambda: "t", session=s)
    assert g.messages_with_verb("F", 104) == ["m9"]
    assert s.calls[0][2]["$filter"] == ("singleValueExtendedProperties/any(ep: ep/id eq "
                                        "'Integer 0x1081' and cast(ep/value, Edm.Int32) eq 104)")
    assert g.message_verbs(["m1", "m2", "m3"]) == {"m1": 102, "m2": None}
    batch = s.calls[1][3]["requests"]
    assert len(batch) == 3 and batch[0]["url"].startswith("/me/messages/m1?$select=id&$expand=")
    assert "ImmutableId" in batch[0]["headers"]["Prefer"]


class VerbGraph:
    """A server where m1 was replied to, m2 forwarded; delta pages as given."""

    def __init__(self, pages):
        self.pages, self.verbs = pages, {"m1": 102, "m2": 104}
        self.backfills = self.lookups = 0

    def messages_delta(self, folder_id, link, since):
        yield from self.pages

    def messages_with_verb(self, folder_id, verb):
        self.backfills += 1
        return [m for m, v in self.verbs.items() if v == verb]

    def message_verbs(self, ids):
        self.lookups += 1
        return {m: self.verbs.get(m) for m in ids}


def test_reply_marks_backfilled_then_kept_current(tmp_path):
    st = make_store(tmp_path)
    g = VerbGraph([([msg("m1"), msg("m2"), msg("m3")], "L1")])
    eng = SyncEngine(st, g, "a")
    eng.sync_folder("F")  # first sync: back-fill (one query per kind of mark)
    marks = {m: st.message_row(m)["last_verb"] for m in ("m1", "m2", "m3")}
    assert marks == {"m1": 102, "m2": 104, "m3": None} and g.backfills == 3
    g.verbs["m3"] = 103  # replied to all in Outlook: the delta reports m3 changed
    g.pages = [([msg("m3", subject="RE: whatever")], "L2")]
    eng.sync_folder("F")
    assert st.message_row("m3")["last_verb"] == 103
    assert g.backfills == 3 and g.lookups == 1  # only the changed message was looked up
    g.pages = [([msg("m4")], "L3")]  # new mail: nothing to look up
    eng.sync_folder("F")
    assert g.lookups == 1


def test_reply_marks_never_fail_the_sync(tmp_path):
    st = make_store(tmp_path)

    class Broken(VerbGraph):
        def messages_with_verb(self, folder_id, verb):
            raise GraphError("filter not supported", 400)

    eng = SyncEngine(st, Broken([([msg("m1")], "L1")]), "a")
    assert eng.sync_folder("F").upserted == 1 and eng.verbs_refused
    assert st.get_state("verbs:F") is None  # tried again next time


def test_meeting_messages_are_marked_by_graphs_type(tmp_path):
    from waddlemail.store import meeting_kind
    assert meeting_kind("#microsoft.graph.eventMessageRequest") == "eventMessageRequest"
    assert meeting_kind("#microsoft.graph.eventMessage") == "eventMessage"
    assert meeting_kind("#microsoft.graph.eventMessageResponse") == "eventMessageResponse"
    assert meeting_kind("#microsoft.graph.message") is None and meeting_kind(None) is None
    st = make_store(tmp_path, msg("m1", **{"@odata.type": "#microsoft.graph.eventMessageRequest"}),
                    msg("m2"))
    assert st.message_row("m1")["meeting"] == "eventMessageRequest"
    assert st.message_row("m2")["meeting"] is None
    st.apply_message_changes("F", [{"id": "m1", "isRead": True}])  # a later partial update
    assert st.message_row("m1")["meeting"] == "eventMessageRequest"
    st.set_meeting("m2", "eventMessage")  # learned by opening it
    assert st.message_row("m2")["meeting"] == "eventMessage"


def test_adding_the_meeting_column_keeps_the_sync_cursors(tmp_path):
    """An upgrade that only adds a column must not make everything download again."""
    import sqlite3
    st = make_store(tmp_path, msg("m1"), msg("m2"))
    st.set_state("delta:F", "CURSOR")
    st.set_state("window:F", "2026-05-01T00:00:00Z")
    st.conn.commit()
    st.conn.close()
    raw = sqlite3.connect(tmp_path / "m.db")
    try:
        raw.execute("ALTER TABLE messages DROP COLUMN meeting")  # a database from before
    except sqlite3.OperationalError:
        import pytest
        pytest.skip("this SQLite can't drop a column")
    raw.commit()
    raw.close()
    st2 = Store(tmp_path / "m.db")
    assert "meeting" in {r["name"] for r in st2.conn.execute("PRAGMA table_info(messages)")}
    assert st2.get_state("delta:F") == "CURSOR" and st2.get_state("window:F")  # kept
    assert st2.message_row("m1")["meeting"] is None


class TypesGraph:
    """message_types replays fixed pages."""

    def __init__(self, *pages):
        self.pages, self.asked = pages, 0

    def message_types(self, folder_id):
        self.asked += 1
        yield from self.pages


def test_meeting_messages_are_learned_once_per_folder(tmp_path):
    st = make_store(tmp_path, msg("m1"), msg("m2"), msg("m3"))
    said = []
    g = TypesGraph({"m1": "eventMessageRequest"}, {"m3": "eventMessage", "gone": "eventMessage"})
    eng = SyncEngine(st, g, "a")
    eng.progress = said.append
    assert eng.backfill_meetings("F") == 2  # ("gone" isn't stored: nothing to mark)
    assert [st.message_row(m)["meeting"] for m in ("m1", "m2", "m3")] == [
        "eventMessageRequest", None, "eventMessage"]
    assert said and "Inbox" in said[-1]
    assert eng.backfill_meetings("F") == 0 and g.asked == 1  # not asked again


def test_a_full_sync_already_knows_the_types(tmp_path):
    st = make_store(tmp_path)
    g = PagesGraph([([msg("m1", **{"@odata.type": "#microsoft.graph.eventMessageRequest"})],
                     "L1")])
    eng = SyncEngine(st, g, "a")
    eng.sync_folder("F")
    assert st.message_row("m1")["meeting"] == "eventMessageRequest"
    assert eng.backfill_meetings("F") == 0  # (no graph.message_types: it never asks)


def test_a_sync_says_how_far_it_has_got(tmp_path):
    st = make_store(tmp_path)
    g = PagesGraph([([msg("m1"), msg("m2")], None), ([msg("m3")], "L1")])
    eng = SyncEngine(st, g, "a")
    said = []
    eng.progress = said.append
    eng.sync_folder("F")
    assert said[0] == "Syncing Inbox… 2 messages" and said[1] == "Syncing Inbox… 3 messages"
    eng.progress = lambda text: 1 / 0  # a broken status bar never stops a sync
    eng.sync_folder("F")


def test_message_types_asks_for_ids_only_and_keeps_the_meeting_ones():
    from tests.test_mailboxes import FakeResponse

    class S:
        def __init__(self):
            self.calls = []

        def request(self, method, url, params=None, **kw):
            self.calls.append((url.split("v1.0")[1], params))
            if len(self.calls) == 1:
                return FakeResponse(200, {"value": [
                    {"id": "a", "@odata.type": "#microsoft.graph.eventMessageRequest"},
                    {"id": "b"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"})
            return FakeResponse(200, {"value": [
                {"id": "c", "@odata.type": "#microsoft.graph.eventMessage"}]})

    s = S()
    pages = list(GraphClient(lambda: "t", session=s).message_types("F"))
    assert pages == [{"a": "eventMessageRequest"}, {"c": "eventMessage"}]
    assert s.calls[0] == ("/me/mailFolders/F/messages", {"$select": "id", "$top": 500})
