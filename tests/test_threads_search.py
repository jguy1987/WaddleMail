"""Conversations, date headings, body text extraction and full-text search (no network)."""
from __future__ import annotations

import datetime as dt
import os

import pytest

from waddlemail.graph import GraphClient
from waddlemail.store import Store
from waddlemail.text import body_text

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_body_text_strips_markup():
    html = ("<html><head><style>p{color:red}</style><title>x</title></head><body>"
            "<p>Hello&nbsp;<b>there</b></p><script>bad()</script><div>Second&amp;line</div>")
    assert body_text("html", html) == "Hello there\nSecond&line"
    assert body_text("text", "plain  text") == "plain text"


def store_with(tmp_path, *items):
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.upsert_folders("a", [{"id": "F", "displayName": "Inbox"}])
    st.apply_message_changes("F", list(items))
    return st


def msg(mid, subject="s", preview="", conv=None, when="2026-11-10T10:00:00Z", **kw):
    return {"id": mid, "subject": subject, "bodyPreview": preview, "conversationId": conv,
            "receivedDateTime": when, **kw}


def test_full_text_search_in_cached_bodies(tmp_path):
    st = store_with(tmp_path, msg("m1"), msg("m2"))
    st.put_body("m1", "html", "<p>The <i>kangaroos</i> are restless</p>", 10**8)
    ids = lambda q, **kw: {r["id"] for r in st.list_messages("F", search=q, **kw)}  # noqa: E731
    assert ids("kangaroo") == {"m1"}            # word prefix in the body
    assert ids("kangaroo restless") == {"m1"}
    assert ids("kangaroo zebra") == set()
    st.put_body("m1", "text", "nothing here", 10**8)  # replaced body: old words gone
    assert ids("kangaroo") == set()
    st.clear_bodies()
    assert ids("nothing") == set()               # evicted bodies leave the index


def test_search_ignores_display_window_and_adds_server_hits(tmp_path):
    st = store_with(tmp_path, msg("old", subject="budget", when="2019-01-01T00:00:00Z"),
                    msg("new", subject="lunch"))
    since = "2026-01-01T00:00:00Z"
    assert [r["id"] for r in st.list_messages("F", since=since)] == ["new"]
    assert [r["id"] for r in st.list_messages("F", since=since, search="budget")] == ["old"]
    hits = st.list_messages("F", search="zzz", also_ids=["new"])
    assert [r["id"] for r in hits] == ["new"]


def test_existing_bodies_are_indexed_on_upgrade(tmp_path):
    st = store_with(tmp_path, msg("m1"))
    st.put_body("m1", "text", "platypus", 10**8)
    st.conn.execute("DROP TABLE body_fts")
    st.conn.commit()
    st2 = Store(tmp_path / "m.db")
    assert [r["id"] for r in st2.list_messages("F", search="platypus")] == ["m1"]


def test_server_search_request():
    from tests.test_mailboxes import FakeResponse

    class Session:
        def request(self, method, url, params=None, headers=None, **kw):
            self.params, self.headers = params, headers
            return FakeResponse(200, {"value": [{"id": "x"}]})

    s = Session()
    out = GraphClient(lambda: "t", session=s, user="box@x.com").search_messages(
        "F", ['budget"', "AND", "q4"])
    assert out == [{"id": "x"}]
    assert s.params["$search"] == '"budget AND and AND q4"'
    assert 'IdType="ImmutableId"' in s.headers["Prefer"]


def test_date_groups():
    from waddlemail.ui.message_list import date_group
    today = dt.date(2026, 11, 18)  # a Wednesday; weeks start Monday (16th)

    def label(day):
        return date_group(f"{day}T12:00:00", today=today, first_weekday=1)[1]

    assert label("2026-11-18") == "Today"
    assert label("2026-11-17") == "Yesterday"
    assert label("2026-11-16") == "This week"
    assert label("2026-11-10") == "Last week"
    assert label("2026-11-02") == "This month"
    assert label("2026-10-05") == "Last month"
    assert label("2026-09-30") == "Older"


def test_conversations_and_headings(qapp, tmp_path):
    from waddlemail.ui.message_list import GROUP, MESSAGE, THREAD, MessageModel, thread_subject
    assert thread_subject("RE: Fwd: AW: Budget") == "Budget"
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda h: (now - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    st = store_with(
        tmp_path,
        msg("a1", "Budget", conv="c", when=iso(50), isRead=True,
            **{"from": {"emailAddress": {"name": "Ann"}}}),
        msg("a2", "RE: Budget", conv="c", when=iso(0.5), isRead=False, hasAttachments=True,
            **{"from": {"emailAddress": {"name": "Bob"}}}),
        msg("b1", "Solo", conv="d", when=iso(0.2), isRead=True))
    model = MessageModel()
    model.set_rows(st.list_messages("F"), outgoing=False, threaded=True, grouped=True)
    groups = model.root.children
    assert all(g.kind == GROUP for g in groups) and groups[0].label == "Today"
    kinds = {n.kind for n in groups[0].children}
    assert kinds == {THREAD, MESSAGE}                       # a2's thread and the solo b1
    thread = next(n for n in groups[0].children if n.kind == THREAD)
    assert thread.row["count"] == 2 and thread.row["participants"] == "Bob, Ann"
    assert thread.row["is_read"] == 0 and thread.row["has_attachments"] == 1
    assert thread.row["newest_id"] == "a2"
    # marking the unread one read updates the conversation row too
    st.set_read("a", ["a2"], True)
    model.update_rows([st.message_row("a2")])
    assert thread.row["is_read"] == 1
    model.set_rows(st.list_messages("F"), outgoing=False, threaded=False, grouped=False)
    assert {n.kind for n in model.root.children} == {MESSAGE}
