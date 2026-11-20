"""Drafts: opening one to go on writing it, and not showing a draft's old text after it was
changed (or sent) from another client."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402

from waddlemail.attachments import Attachment  # noqa: E402
from waddlemail.outbox import deliver, make_payload  # noqa: E402
from waddlemail.store import Store  # noqa: E402


def msg(mid, **kw):
    item = {"id": mid, "subject": f"subject {mid}", "isRead": True,
            "from": {"emailAddress": {"name": "Me", "address": "me@x.com"}},
            "receivedDateTime": "2026-11-18T10:00:00Z"}
    item.update(kw)
    return item


def two_folder_store(tmp_path) -> Store:
    st = Store(tmp_path / "m.db")
    st.add_mailbox("a", "a", None, "me@x.com")
    st.upsert_folders("a", [{"id": "D", "displayName": "Drafts", "wellKnownName": "drafts"},
                            {"id": "S", "displayName": "Sent Items",
                             "wellKnownName": "sentitems"},
                            {"id": "I", "displayName": "Inbox", "wellKnownName": "inbox"}])
    return st


# -- a draft's cached text never outlives a change -------------------------------------------
def test_a_draft_changed_elsewhere_is_read_again(tmp_path):
    st = two_folder_store(tmp_path)
    st.apply_message_changes("D", [msg("d1")])
    st.put_body("d1", "html", "<p>first version</p>", 10**6)
    st.apply_message_changes("D", [msg("d1", subject="edited in Outlook")])  # a delta says so
    assert not st.has_body("d1")  # (so it is fetched again, not shown stale)
    assert st.message_row("d1")["subject"] == "edited in Outlook"


def test_a_draft_sent_elsewhere_does_not_keep_its_draft_text(tmp_path):
    """Sent from Outlook on the web, the same message turns up in Sent Items: its cached
    body, from when it was a draft here, must not be shown."""
    st = two_folder_store(tmp_path)
    st.apply_message_changes("D", [msg("d1")])
    st.put_body("d1", "html", "<p>the draft as saved here</p>", 10**6)
    st.apply_message_changes("S", [msg("d1", subject="final subject")])  # re-homed by Sent's sync
    assert st.message_row("d1")["folder_id"] == "S" and not st.has_body("d1")
    st.apply_message_changes("D", [{"id": "d1", "@removed": {"reason": "deleted"}}])  # Drafts'
    assert st.message_row("d1") is not None  # (still the one in Sent Items)


def test_other_mail_keeps_its_cached_text_when_it_changes(tmp_path):
    st = two_folder_store(tmp_path)
    st.apply_message_changes("I", [msg("m1", isRead=False)])
    st.put_body("m1", "html", "<p>received</p>", 10**6)
    st.apply_message_changes("I", [msg("m1", isRead=True)])  # only read; the text can't change
    assert st.has_body("m1")


# -- opening a draft ---------------------------------------------------------------------
class DraftGraph:
    """What the server has for the draft, and what was done to it."""

    def __init__(self, data, attachments=()):
        self.data, self._attachments, self.calls = data, list(attachments), []

    def get_draft(self, mid):
        self.calls.append(("get_draft", mid))
        return self.data

    def message_path(self, mid):
        return f"/me/messages/{mid}"

    def attachments(self, item):
        return list(self._attachments)

    def create_draft(self, message):
        self.calls.append(("create_draft",))
        return {"id": "NEW"}

    def create_response(self, original, kind):
        self.calls.append(("create_response",))
        return {"id": "NEW"}

    def update_message(self, mid, message):
        self.calls.append(("update_message", mid, message["subject"]))

    def send_draft(self, mid):
        self.calls.append(("send_draft", mid))

    def sent_with_mark(self, mark):
        return False

    def add_attachment(self, *a, **kw):
        self.calls.append(("add_attachment",))

    def delete_attachment(self, *a):
        self.calls.append(("delete_attachment",))


def discard(win):
    """Close a compose window without the 'Save to Drafts?' question (which waits for a click)."""
    win.sent = True
    win.close()


def person(name, addr):
    return {"emailAddress": {"name": name, "address": addr}}


def open_draft(graph, bodies=None):
    """Run MainWindow.edit_draft on a stand-in main window; returns (compose window, fake)."""
    from waddlemail.ui.compose_window import ComposeWindow
    from waddlemail.ui.main_window import MainWindow

    box = SimpleNamespace(id="a", account_id="acct", shared=False, graph=graph,
                          bodies=bodies or SimpleNamespace(with_pictures=lambda mid, c: c))
    made = []

    def compose(**kw):
        win = ComposeWindow([("acct", "me@x.com")], [], {"html": True}, lambda aid: None)
        made.append(win)
        return win

    fake = SimpleNamespace(
        _windows=set(), store=SimpleNamespace(message_row=lambda mid: {"folder_id": "D"}),
        mailboxes=SimpleNamespace(for_folder=lambda fid: box), _compose=compose,
        _run=lambda job, done, **kw: done(job()))
    MainWindow.edit_draft(fake, "d1")
    return (made[0] if made else None), fake


def test_a_draft_opens_with_what_was_written(qapp):
    graph = DraftGraph(
        {"subject": "Quarterly plan", "hasAttachments": True,
         "body": {"contentType": "html", "content": "<p>Hello <b>team</b></p>"},
         "toRecipients": [person("Ann Lee", "ann@x.com")],
         "ccRecipients": [person("", "cc@x.com")], "bccRecipients": [person("Bo", "bo@x.com")]},
        attachments=[{"id": "A1", "name": "plan.pdf", "size": 10},
                     {"id": "P1", "name": "logo.png", "size": 5, "isInline": True}])
    win, _ = open_draft(graph)
    assert win.subject.text() == "Quarterly plan"
    assert win.to.addresses() == ["Ann Lee <ann@x.com>"] and win.cc.addresses() == ["cc@x.com"]
    assert win.bcc.addresses() == ["Bo <bo@x.com>"]
    assert "Hello" in win.editor.edit.toPlainText()
    assert win.draft_id == "d1" and win.mailbox_id == "a"
    assert [(a.name, a.remote_id) for a in win.attachments.items()] == [("plan.pdf", "A1")]
    assert not win.is_dirty()  # (closing it unchanged asks nothing)
    discard(win)


def test_a_plain_text_draft_stays_plain(qapp):
    win, _ = open_draft(DraftGraph({"subject": "s", "body": {"contentType": "text",
                                                            "content": "just words"}}))
    assert win.editor.is_plain() and win.editor.text().strip() == "just words"
    discard(win)


def test_sending_a_draft_updates_that_draft_instead_of_making_another(qapp):
    graph = DraftGraph({"subject": "Quarterly plan", "hasAttachments": True,
                        "body": {"contentType": "html", "content": "<p>Hello</p>"},
                        "toRecipients": [person("Ann Lee", "ann@x.com")]},
                       attachments=[{"id": "A1", "name": "plan.pdf", "size": 10}])
    win, _ = open_draft(graph)
    win.subject.setText("Quarterly plan v2")  # edited here
    p = make_payload("a", win.outgoing(), win.kind, None, win.draft_id,
                     win.attachments.items(), win.originals_known, win.has_had_attachments,
                     win.had_pictures)
    graph.calls.clear()
    deliver(graph, p)
    assert graph.calls == [("update_message", "d1", "Quarterly plan v2"), ("send_draft", "d1")]
    discard(win)  # (no create_draft/create_response, and the attachment was not touched)


def test_opening_a_draft_twice_shows_the_same_window(qapp):
    from waddlemail.ui.compose_window import ComposeWindow
    from waddlemail.ui.main_window import MainWindow

    win, fake = open_draft(DraftGraph({"subject": "s", "body": {"content": ""}}))
    fake._windows.add(win)
    graph = DraftGraph({"subject": "other"})
    fake.mailboxes = SimpleNamespace(for_folder=lambda fid: SimpleNamespace(graph=graph))
    MainWindow.edit_draft(fake, "d1")  # again
    assert graph.calls == [] and isinstance(win, ComposeWindow)  # nothing fetched, no 2nd window
    discard(win)


def test_double_click_in_drafts_edits_and_elsewhere_reads(qapp):
    from waddlemail.ui.main_window import MainWindow

    opened = []
    folders = {"D": {"well_known": "drafts"}, "I": {"well_known": "inbox"}}
    fake = SimpleNamespace(
        outbox_id=MainWindow.outbox_id, _current_folder="D",
        store=SimpleNamespace(message_row=lambda mid: {"folder_id": "D" if mid == "d1" else "I"},
                              folder_row=lambda fid: folders[fid]),
        mailboxes=SimpleNamespace(for_folder=lambda fid: SimpleNamespace(shared=False)),
        edit_draft=opened.append)
    fake._is_draft = lambda row: MainWindow._is_draft(fake, row)
    MainWindow.open_message(fake, "d1")
    assert opened == ["d1"]
    # a message elsewhere goes on down the ordinary path (which needs a real window: stop at
    # the first thing it does after the draft check)
    fake.store.message_row = lambda mid: {"folder_id": "I"}
    try:
        MainWindow.open_message(fake, "m1")
    except AttributeError:
        pass  # (it got past the draft check and began building a message window)
    assert opened == ["d1"]


def test_closing_asks_only_when_something_would_be_lost(qapp):
    from waddlemail.ui.compose_window import ComposeWindow

    win = ComposeWindow([("acct", "me@x.com")], [], {"html": True}, lambda aid: None)
    assert not win.is_dirty()                      # nothing written
    win.subject.setText("Lunch?")
    assert win.is_dirty()                          # a subject alone is worth keeping
    win.mark_saved("d9")                           # saved to Drafts
    assert not win.is_dirty()                      # (it used to ask again)
    win.subject.setText("Lunch on Friday?")        # then changed, with the body untouched
    assert win.is_dirty()                          # (it used to be lost)
    win.mark_saved("d9")
    win.to.setText("ann@x.com; ")
    assert win.is_dirty()
    discard(win)
