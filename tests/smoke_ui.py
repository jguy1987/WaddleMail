"""Drive the real main window offscreen with sample mail and no network.

    QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu" \
        python tests/smoke_ui.py

(--disable-gpu: without it QtWebEngine's GPU thread can crash on the offscreen platform.)
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
app = QApplication(sys.argv)

from waddlemail import graph  # noqa: E402
from waddlemail.mailboxes import MailboxManager  # noqa: E402
from waddlemail.settings import Settings  # noqa: E402
from waddlemail.store import Store  # noqa: E402
from waddlemail.ui.main_window import MainWindow  # noqa: E402
from waddlemail.ui.message_list import COL, NODE_ROLE  # noqa: E402
from waddlemail.notify import Notifier  # noqa: E402
import waddlemail.ui.main_window as main_window_module  # noqa: E402


class RecordingNotifier(Notifier):
    """Stands in for desktop notifications, so the test doesn't pop any up."""

    def __init__(self):
        super().__init__()
        self.sent, self.badges = [], []

    def notify(self, summary, body, key, origin="", kind="mail"):
        self.sent.append((summary, body, key, origin))
        self.kinds = getattr(self, "kinds", []) + [kind]

    def set_badge(self, count):
        self.badges.append(count)


main_window_module.make_notifier = lambda tray: RecordingNotifier()

from tests.test_contacts import FakeGraph as FakeContacts  # noqa: E402
_fake_contacts = FakeContacts()
saved_contacts: list[dict] = []
graph.GraphClient.contacts = lambda self, folder_id=None: _fake_contacts.contacts(folder_id)
graph.GraphClient.contact_folders = lambda self: _fake_contacts.contact_folders()
graph.GraphClient.directory_users = lambda self, limit=50_000: _fake_contacts.directory_users()
graph.GraphClient.contact_groups = lambda self: _fake_contacts.contact_groups()


def _save_contact(self, fields, contact_id=None, folder_id=None):
    saved_contacts.append(fields)
    return {"id": contact_id or "new-contact", **fields}


graph.GraphClient.save_contact = _save_contact
graph.GraphClient.delete_contact = lambda self, cid: None


def offline(self, url, params=None, headers=None, retries=5, method="GET", json=None, **kw):
    raise graph.GraphError("offline", 503)


def server_search(self, folder_id, words, limit=250):
    """The server knows one old message that was never synced."""
    if "kangaroo" in [w.lower() for w in words]:
        return [{"id": "old1", "parentFolderId": folder_id, "subject": "Kangaroo report 2019",
                 "receivedDateTime": "2019-03-01T10:00:00Z", "isRead": True,
                 "from": {"emailAddress": {"name": "Archive", "address": "a@x.com"}}}]
    return []


class FakeFolders:
    """The server's folder tree, for create / rename / move / delete."""

    def __init__(self):
        self.folders = {
            "in": {"id": "in", "displayName": "Inbox", "wellKnownName": "inbox",
                   "unreadItemCount": 3},
            "del": {"id": "del", "displayName": "Deleted Items", "wellKnownName": "deleteditems"},
            "out": {"id": "out", "displayName": "Outbox", "wellKnownName": "outbox"},
            "sync": {"id": "sync", "displayName": "Sync Issues", "wellKnownName": "syncissues"},
            "conf": {"id": "conf", "displayName": "Conflicts", "wellKnownName": "conflicts",
                     "parentFolderId": "sync"},
        }
        self.n = 0

    def list_folders(self, client):
        return [dict(f) for f in self.folders.values()]

    def create(self, client, name, parent_id=None):
        self.n += 1
        fid = f"new{self.n}"
        self.folders[fid] = {"id": fid, "displayName": name, "parentFolderId": parent_id}
        return {"id": fid}

    def rename(self, client, fid, name):
        self.folders[fid]["displayName"] = name

    def move(self, client, fid, dest):
        self.folders[fid]["parentFolderId"] = {"deleteditems": "del"}.get(dest, dest)

    def delete(self, client, fid):
        doomed = {fid}
        while True:
            more = {k for k, f in self.folders.items() if f.get("parentFolderId") in doomed}
            if more <= doomed:
                break
            doomed |= more
        for k in doomed:
            del self.folders[k]


server = FakeFolders()
graph.GraphClient._request = offline
graph.GraphClient.search_messages = server_search
graph.GraphClient.list_folders = lambda self: server.list_folders(self)
graph.GraphClient.create_folder = lambda self, name, parent_id=None: server.create(self, name, parent_id)
graph.GraphClient.rename_folder = lambda self, fid, name: server.rename(self, fid, name)
graph.GraphClient.move_folder = lambda self, fid, dest: server.move(self, fid, dest)
graph.GraphClient.delete_folder = lambda self, fid: server.delete(self, fid)
server_moves: list[tuple[str, str]] = []
graph.GraphClient.move_message = lambda self, mid, dest: server_moves.append((mid, dest)) or {}
graph.GraphClient.messages_before = lambda self, fid, before, limit=500: [
    {"id": f"older{i}", "parentFolderId": fid, "subject": f"Old {i}", "isRead": True,
     "receivedDateTime": f"2018-0{i}-01T00:00:00Z"} for i in (1, 2)] if fid == "in" else []
graph.GraphClient.message_sizes = lambda self, fid: iter([
    {m: 1024 * (i + 3) for i, m in enumerate(("m1", "m2", "m3", "m4", "m5", "m6"))}])
graph.GraphClient.copy_message = lambda self, mid, dest: {
    "id": f"copy-of-{mid}", "parentFolderId": dest, "subject": "copied", "isRead": True,
    "receivedDateTime": "2026-11-01T10:00:00Z"}

# Dialogs would block the test: answer Yes, and record warnings.
from PySide6.QtWidgets import QLineEdit, QMessageBox  # noqa: E402
warnings: list[str] = []
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.warning = staticmethod(lambda parent, title, text, *a: warnings.append(text))


class FakeAuth:
    def accounts(self):
        return [{"home_account_id": "a", "username": "jordan@contoso.com"}]

    def get_token(self, account_id=None, interactive=True):
        return "t"

    def sign_out(self, account_id=None):
        pass


tmp = Path(tempfile.mkdtemp())
st = Store(tmp / "mail.db")
st.add_mailbox("a", "a", None, "jordan@contoso.com")
st.upsert_folders("a", [{"id": "in", "displayName": "Inbox", "wellKnownName": "inbox",
                         "unreadItemCount": 3}])
now = dt.datetime.now(dt.timezone.utc)


def m(i, subject, name, hours, read=False, att=False, flag="notFlagged", conv=None):
    return {"id": i, "subject": subject, "conversationId": conv,
            "from": {"emailAddress": {"name": name, "address": f"{name.split()[0]}@x.com"}},
            "receivedDateTime": (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "isRead": read, "hasAttachments": att, "flag": {"flagStatus": flag}}


st.apply_message_changes("in", [
    m("m1", "Q4 budget review", "Priya Shah", 1, att=True),
    m("m2", "Re: Offsite agenda", "Marcus Webb", 3, read=True, conv="c1"),
    m("m6", "Offsite agenda", "Dana Cho", 30, read=True, conv="c1"),
    m("m3", "Build failed", "CI Bot", 20),
    m("m4", "Contract draft", "Alex Kim", 80, read=True, att=True),
    m("m5", "Newsletter", "Contoso News", 200),
])
st.put_body("m1", "html", "<p>Final numbers attached.</p>", 10**8)
st.put_body("m5", "html", "<style>p{}</style><p>Our <b>kangaroo</b> mascot</p>", 10**8)

w = MainWindow(st, MailboxManager(st, FakeAuth()), Settings(str(tmp / "settings.ini")))
w.resize(1280, 760)
w.show()
view = w.msg_list
failures = 0


def check(cond, what):
    global failures
    print(("ok   " if cond else "FAIL ") + what)
    failures += not cond


def wait(ms: int):
    """Let the UI and background workers run. (QTest.qWait keeps Python's GIL, which stalls
    the worker threads.)"""
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.02)


def nodes():
    """(index, node) for every row of the view, in order."""
    return [(i, i.data(NODE_ROLE)) for i in view._walk()]


def ids():
    return [n.row["id"] for _, n in nodes() if n.kind == "message"]


def unread():
    return {r["id"] for r in st.list_messages("in") if not r["is_read"]}


def find(mid, col=COL["subject"]):
    for idx, n in nodes():
        if n.kind == "message" and n.row["id"] == mid:
            return idx.siblingAtColumn(col)
    raise LookupError(mid)


def point(mid, col):
    return view.visualRect(find(mid, col)).center()


def click(mid, col=COL["subject"]):
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point(mid, col))


def double_click(mid):
    # Raw events: QTest.mouseDClick is unreliable when repeated.
    p = QPointF(point(mid, COL["subject"]))
    for t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
              QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
              QEvent.Type.MouseButtonRelease):
        buttons = (Qt.MouseButton.NoButton if t == QEvent.Type.MouseButtonRelease
                   else Qt.MouseButton.LeftButton)
        QApplication.sendEvent(view.viewport(), QMouseEvent(
            t, p, QPointF(view.viewport().mapToGlobal(p)), Qt.MouseButton.LeftButton,
            buttons, Qt.KeyboardModifier.NoModifier))


def run():
    try:
        checks()
    except Exception:
        import traceback
        traceback.print_exc()
        app.exit(2)


def checks():
    kinds = [n.kind for _, n in nodes()]
    check(kinds[0] == "group" and nodes()[0][1].label == "Today", "date headings, newest first")
    thread = next(n for _, n in nodes() if n.kind == "thread")
    check(thread.row["count"] == 2 and thread.row["subject"] == "Offsite agenda"
          and thread.row["participants"] == "Marcus Webb, Dana Cho",
          "conversation row: count, subject without Re:, participants")
    check(ids() == ["m1", "m2", "m6", "m3", "m4", "m5"], "newest first, within headings")
    t_idx = next(i for i, n in nodes() if n.kind == "thread")
    check(not view.isExpanded(t_idx), "conversations start collapsed")
    r = view.visualRect(t_idx.siblingAtColumn(COL["subject"]))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(r.left() + 8, r.center().y()))
    check(view.isExpanded(t_idx), "arrow expands a conversation")
    view.selectionModel().select(t_idx, view.selectionModel().SelectionFlag.ClearAndSelect
                                 | view.selectionModel().SelectionFlag.Rows)
    check(view.shown_id() == "m2" and set(view.selected_ids()) == {"m2", "m6"},
          "selecting a conversation shows its newest message, acts on all")

    click("m1")
    wait(200)
    check("m1" not in unread(), "selecting in the reading pane marks read")
    check(st.folder_row("in")["unread"] == 2, "folder unread count drops")
    check(len(st.pending_changes("a")) == 1, "change queued for the server")

    click("m3", COL["read"])
    check("m3" not in unread() and view.selected_ids() == ["m1"],
          "read dot toggles without changing the selection")
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_M)
    QTest.keyClick(view, Qt.Key.Key_S)
    check("m1" in unread() and st.message_row("m1")["flag"] == "flagged", "M and S keys")

    view.sortByColumn(COL["attach"], Qt.SortOrder.DescendingOrder)
    check(not any(n.kind == "group" for _, n in nodes()), "no date headings unless by date")
    check(set(ids()[:2]) == {"m1", "m4"}, "sort by attachment")
    view.sortByColumn(COL["received"], Qt.SortOrder.DescendingOrder)
    check(nodes()[0][1].kind == "group", "headings back when sorted by date")

    # (this test's mail sync is offline, so run the size step the sync job runs after it)
    w.mailboxes.all()[0].sync.fill_sizes("in")
    w._refresh_messages()
    check(st.message_row("m1")["size"] == 3072
          and view.model_.text(st.message_row("m1"), "size") == "3 KB",
          "message sizes are fetched separately and shown (Size column)")
    w._focus_filter()
    QTest.keyClicks(w.filter_bar.search, "contract alex")
    wait(400)
    check(ids() == ["m4"], "quick filter: every word matches somewhere")
    w.filter_bar.search.clear()
    QTest.keyClicks(w.filter_bar.search, "kangaroo")
    wait(1500)  # local at once, then the server search (after a pause in typing)
    check("m5" in ids(), "full-text: a word in a cached body")
    check("old1" in ids() and "on server" in w.filter_bar.count.text(),
          "server search adds mail that was never synced")
    QTest.keyClick(w.filter_bar.search, Qt.Key.Key_Escape)
    w.filter_bar.unread.setChecked(True)
    check(set(ids()) == unread(), "unread toggle")
    w.filter_bar.unread.setChecked(False)

    w.act_threads.setChecked(False)
    check(not any(n.kind == "thread" for _, n in nodes()), "conversations can be turned off")
    w.act_threads.setChecked(True)

    w.set_reading_pane("right")
    wait(200)
    check(view.compact and view.header().isHidden(), "right pane: compact rows")
    r = view.visualRect(find("m5"))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(r.left() + 10, r.top() + 8))
    check("m5" not in unread(), "compact: dot click toggles read")

    w.set_reading_pane("off")
    wait(300)
    check(not view.compact and w.msg_view.isHidden(), "reading pane off")
    click("m3")
    w.mark_read(["m3"], False)
    click("m1")
    click("m3")
    wait(200)
    check("m3" in unread(), "pane off: selecting doesn't mark read")
    double_click("m3")
    wait(300)
    check(len(w._windows) == 1 and "m3" not in unread(), "double-click opens and marks read")

    folder_checks()
    move_checks()
    integration_checks()
    older_mail_checks()
    status_checks()
    contacts_checks()
    contact_edit_checks()
    calendar_checks()
    recurrence_checks()
    reminder_checks()
    new_button_checks()
    compose_checks()

    w.close()
    saved = Settings(str(tmp / "settings.ini"))
    check(saved.get("view/reading_pane") == "off" and not saved.get("list/header").isEmpty(),
          "layout saved on exit")
    app.exit(1 if failures else 0)


def type_in_editor(text: str, key=Qt.Key.Key_Return) -> bool:
    wait(100)
    # (offscreen windows never get keyboard focus, so look for the editor itself)
    editors = [e for e in w.folder_tree.viewport().findChildren(QLineEdit) if e.isVisible()]
    if not editors:
        return False
    editor = editors[0]
    editor.selectAll()
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, key)
    wait(400)
    return True


def folder_named(name):
    return next((r for r in st.folder_rows() if r["name"] == name), None)


def folder_checks():
    pane = w.folder_tree
    pane.select_folder("in")
    wait(100)
    pane.start_new_folder()
    check(type_in_editor("Projects"), "New folder: name edited in place")
    made = folder_named("Projects")
    check(made is not None and made["parent_id"] == "in", "new subfolder created under Inbox")
    check(w._current_folder == (made and made["id"]), "the new folder is selected")

    pane.select_folder("in")
    pane.start_new_folder()
    type_in_editor("projects")
    check(warnings and "already a folder" in warnings[-1], "duplicate name refused")
    pane.start_new_folder()
    type_in_editor("Ignored", key=Qt.Key.Key_Escape)
    check(folder_named("Ignored") is None and pane._edit is None, "Esc cancels a new folder")

    pane.select_folder(made["id"])
    pane.setFocus()
    QTest.keyClick(pane, Qt.Key.Key_F2)
    check(type_in_editor("Clients"), "F2 renames in place")
    check(st.folder_row(made["id"])["name"] == "Clients", "renamed on the server and here")

    pane.select_folder("in")
    QTest.keyClick(pane, Qt.Key.Key_F2)
    wait(100)
    check(pane._edit is None, "Inbox can't be renamed")

    pane.select_folder(made["id"])
    QTest.keyClick(pane, Qt.Key.Key_Delete)
    wait(400)
    check(st.folder_row(made["id"])["parent_id"] == "del", "Delete moves it to Deleted Items")

    for fid in ("out", "sync", "conf"):
        pane.start_new_folder(pane._find(fid))
        wait(100)
    check(pane._edit is None, "no New subfolder in Outbox, Sync Issues or Conflicts")
    check(pane.can_hold_folders(pane._find("in"))
          and not any(pane.can_hold_folders(pane._find(f)) for f in ("out", "sync", "conf")),
          "the New subfolder menu item is greyed out there")
    pane.select_folder("out")
    wait(300)  # (selecting starts a sync, whose offline error would replace the message)
    w.new_folder()
    check(pane._edit is None and "can't contain folders" in w.statusBar().currentMessage(),
          "File > New > Folder says why")
    before = len(warnings)
    w._create_folder("a", "sync", "Sneaky")
    wait(300)
    check(len(warnings) == before + 1 and folder_named("Sneaky") is None,
          "refused before anything is sent")
    check(not pane.can_change(pane._find("sync")), "Sync Issues can't be renamed or deleted")
    pane.select_folder(made["id"])
    pane.select_folder(made["id"])
    QTest.keyClick(pane, Qt.Key.Key_Delete)
    wait(400)
    check(st.folder_row(made["id"]) is None and w._current_folder == "in",
          "deleting it from Deleted Items removes it; Inbox is shown")


def drop(item, payload_type, payload, ctrl=False):
    from PySide6.QtGui import QDropEvent
    from waddlemail.ui import dnd
    mime = dnd.encode(payload_type, payload)
    pos = QPointF(w.folder_tree.visualItemRect(item).center())
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    ev = QDropEvent(pos, Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, mods)
    w.folder_tree.dropEvent(ev)
    wait(400)
    return ev.isAccepted()


def move_checks():
    from waddlemail.ui import dnd
    from waddlemail.ui.folder_picker import FolderPicker
    pane = w.folder_tree
    receipts = server.create(None, "Receipts", "in")["id"]
    travel = server.create(None, "Travel", "in")["id"]
    w._sync_all_folders()
    w._populate_folders()
    pane.select_folder("in")
    wait(300)
    item = pane._find

    check(drop(item(receipts), dnd.MESSAGES, {"folder": "in", "ids": ["m4"]}),
          "drop messages on a folder")
    check(st.message_row("m4")["folder_id"] == receipts and "m4" not in ids(),
          "dropped message moved at once, gone from the list")
    wait(2000)  # the change queue sends it
    check(("m4", receipts) in server_moves, "the move was sent to the server")

    check(drop(item(travel), dnd.MESSAGES, {"folder": "in", "ids": ["m3"]}, ctrl=True),
          "Ctrl-drop copies")
    check(st.message_row("copy-of-m3")["folder_id"] == travel
          and st.message_row("m3")["folder_id"] == "in", "copy made, original stays")

    check(not drop(item("in"), dnd.MESSAGES, {"folder": "in", "ids": ["m1"]}),
          "dropping on its own folder does nothing")

    check(drop(item(receipts), dnd.FOLDER, {"folder": travel}), "drop a folder on a folder")
    check(st.folder_row(travel)["parent_id"] == receipts, "folder moved on the server and here")
    check(not drop(item(travel), dnd.FOLDER, {"folder": receipts}),
          "a folder can't be dropped into its own subfolder")
    check(not drop(item("in"), dnd.FOLDER, {"folder": "in"}), "default folders don't move")
    check(not drop(item("out"), dnd.FOLDER, {"folder": travel})
          and not drop(item("sync"), dnd.FOLDER, {"folder": travel}),
          "no folder can be dropped into Outbox or Sync Issues")

    picker = FolderPicker(st, "a", "Move folder", "Move to:", subtree=receipts,
                          allow_top=True)
    flags = {i: picker.tree.findItems(n, Qt.MatchFlag.MatchRecursive)[0].flags()
             for i, n in ((receipts, "Receipts"), (travel, "Travel"), ("in", "Inbox"))}
    check(not flags[receipts] & Qt.ItemFlag.ItemIsSelectable
          and not flags[travel] & Qt.ItemFlag.ItemIsSelectable
          and flags["in"] & Qt.ItemFlag.ItemIsSelectable,
          "picker: a folder can't go into itself or below")
    picker.filter.setText("inb")
    check(picker.tree.currentItem().text(0) == "Inbox", "picker: typing finds a folder")

    w._pick_folder = lambda *a, **k: receipts  # the picker's answer
    pane.select_folder("in")
    wait(300)
    click("m1")
    w.act_move.trigger()
    wait(200)
    check(st.message_row("m1")["folder_id"] == receipts, "Move to folder… (menu)")
    check(view.current_id() not in (None, "m1"), "the next message is selected after a move")

    w.open_message("m5")
    wait(300)
    win = next(x for x in w._windows if x.message_id == "m5")
    w._pick_folder = lambda *a, **k: travel
    win.act_copy.trigger()
    wait(400)
    check(st.message_row("copy-of-m5") is not None, "message window: Copy to folder")
    win.act_move.trigger()
    wait(200)
    check(st.message_row("m5")["folder_id"] == travel, "message window: Move to folder")


def integration_checks():
    from waddlemail.ui.splash import Splash
    n = w.notifier
    recent = lambda m: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=m)).strftime(  # noqa: E731
        "%Y-%m-%dT%H:%M:%SZ")
    st.apply_message_changes("in", [
        {"id": "n1", "subject": "Lunch?", "isRead": False, "receivedDateTime": recent(1),
         "bodyPreview": "Are you free at noon?",
         "from": {"emailAddress": {"name": "Sam Ortiz", "address": "sam@x.com"}}},
        {"id": "n2", "subject": "Report", "isRead": False, "receivedDateTime": recent(2),
         "from": {"emailAddress": {"name": "Kim Lee", "address": "kim@x.com"}}},
        {"id": "old", "subject": "Ancient", "isRead": False,
         "receivedDateTime": "2020-01-01T00:00:00Z"},
    ])
    n.sent.clear()
    w._notify_new(["n1"])
    check(n.sent and n.sent[-1][0] == "Sam Ortiz" and "Lunch?" in n.sent[-1][1]
          and n.sent[-1][2] == "in|n1" and n.sent[-1][3] == "jordan@contoso.com",
          "one new message: sender, subject + preview, mailbox")
    w._notify_new(["n1", "n2", "old"])
    check(n.sent[-1][0] == "2 new messages" and "Kim Lee: Report" in n.sent[-1][1],
          "several: one summary (old mail left out)")
    w.settings.set("notify/new_mail", False)
    before = len(n.sent)
    w._notify_new(["n1"])
    check(len(n.sent) == before, "notifications can be turned off")
    w.settings.set("notify/new_mail", True)

    w.folder_tree.select_folder(travel_id())
    wait(200)
    n.activated.emit("in|n1")
    wait(200)
    check(w._current_folder == "in" and view.current_id() == "n1",
          "clicking a notification opens its message")

    st.conn.execute("UPDATE folders SET unread = 7 WHERE id = 'in'")
    st.conn.commit()
    w._populate_folders()
    check(n.badges[-1] == 7 and "7 unread" in w.tray.toolTip(),
          "unread count on the task manager badge and tray")

    w.settings.set("tray/close_to_tray", True)
    w.tray.isVisible = lambda: True  # the offscreen platform has no tray
    w.close()
    wait(100)
    check(w.isHidden() and not w._quitting, "close-to-tray hides instead of quitting")
    w.tray.show_window()
    wait(100)
    check(w.isVisible(), "the tray brings it back")
    w.settings.set("tray/close_to_tray", False)

    sp = Splash()
    sp.status("Opening your mail…")
    img = sp.pixmap().toImage()
    check(not img.isNull() and img.width() >= 440, "splash screen renders")
    sp.close()


def older_mail_checks():
    w.folder_tree.select_folder("in")
    wait(300)
    stored = st.message_count("in")
    st.conn.execute("UPDATE folders SET total = ? WHERE id = 'in'", (stored + 2,))
    st.conn.commit()
    w._refresh_messages()
    bar = w.older_bar
    check(bar.isVisible() and "2 older messages" in bar.label.text(),
          "the bar says how much older mail is only on the server")
    bar.button.click()
    wait(500)
    check(st.message_row("older1") is not None and "older2" in ids(),
          "Download older mail fetches it into the list")
    check(not bar.isVisible(), "the bar goes away when everything is here")


def status_checks():
    import threading
    ind = w.status
    check(graph.status_hook == ind.report, "requests report to the status indicator")
    # reported from a worker thread, as real requests are
    threading.Thread(target=graph._report,
                     args=(graph.OFFLINE, "Can't reach the server.")).start()
    wait(200)
    check(ind.state == graph.OFFLINE and ind.dot.color.name() == "#e03131"
          and ind.label.text().startswith("Offline")
          and "Can't reach the server." in ind.toolTip(), "offline: red dot, text, details")
    w.mark_read([ids()[0]], False)
    w.mark_read([ids()[0]], True)  # toggled twice: no change left to send
    w.mark_read([ids()[1]], not st.message_row(ids()[1])["is_read"])
    n = st.pending_count()
    check(n > 0 and f"{n} change{'s' if n != 1 else ''} waiting" in ind.label.text(),
          "offline: changes waiting are counted")
    graph._report(graph.ONLINE)
    wait(100)
    check(ind.dot.color.name() == "#2f9e44" and ind.label.text().startswith("Online"),
          "online: green dot")
    token = ind.begin("Downloading older mail…")
    check(ind.spinner.isVisible() and ind.label.text() == "Downloading older mail…",
          "activity: spinner and what is happening")
    ind.end(token)
    check(not ind.spinner.isVisible(), "spinner stops when it's done")
    wait(2000)  # let pending sends / syncs finish
    w._syncing = False
    QTest.mouseClick(ind, Qt.MouseButton.LeftButton)
    check(ind.label.text() == "Syncing with server…", "clicking it syncs now")
    wait(500)
    ind.synced()
    graph._report(graph.ONLINE)
    wait(100)
    check(ind.label.text().startswith("Up to date ·") or "waiting" in ind.label.text(),
          "idle: up to date with the time of the last sync")


def contacts_checks():
    from waddlemail.contacts import suggestions
    from waddlemail.ui.recipients import RecipientEdit
    view = w.contacts_view
    w.refresh_contacts()
    wait(800)
    w.setFocus()
    QTest.keyClick(w, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    wait(100)
    check(w.pages.currentWidget() is view and w._nav["contacts"].isChecked(),
          "Ctrl+3 / the rail switch to Contacts")
    sources = [view.sources.topLevelItem(i).text(0)
               for i in range(view.sources.topLevelItemCount())]
    check(sources == ["All contacts", "Contact groups", "Organization", "Recent recipients"]
          and view.sources.topLevelItem(0).childCount() == 2,
          "sources: contacts (with folders), groups, directory, recent")
    names = [view.list.topLevelItem(i).text(0) for i in range(view.list.topLevelItemCount())]
    check(names == ["Bo Chan", "Mom Lee", "Priya Shah"], "all personal contacts, A-Z")
    view.search.setText("contoso")
    wait(300)
    check(view.list.topLevelItemCount() == 1, "search by company")
    view.search.clear()
    wait(300)

    view.sources.setCurrentItem(view.sources.topLevelItem(1))  # groups: first one shown
    html_text = view.details.toHtml()
    check("Project Team" in html_text and "ana@x.com" in html_text and "bo@y.org" in html_text,
          "a contact group's members with their addresses")
    composed = []
    real_new_mail = w.new_mail
    w.new_mail = lambda to=None: composed.append(to)
    view.email_selected()
    check(composed and len(composed[-1]) == 3, "email a whole group")

    view.sources.setCurrentItem(view.sources.topLevelItem(2))  # directory
    check(view.list.topLevelItemCount() == 1 and view.list.topLevelItem(0).text(0) == "Ana Ruiz"
          and not view.act_edit.isEnabled() and view.act_add.isEnabled(),
          "directory: read-only, can be added to your contacts")
    w.new_mail = real_new_mail

    w.save_contact({"displayName": "Zed Quinn", "givenName": "Zed", "surname": "Quinn",
                    "emailAddresses": [{"address": "zed@q.io", "name": "Zed Quinn"}]},
                   "", "", "Contacts")
    wait(400)
    check(saved_contacts and st.contact_row("new-contact")["display_name"] == "Zed Quinn",
          "new contact saved on the server and here")

    edit = RecipientEdit(suggestions(st))
    edit.setText("Ann <a@x>; proj")
    comp = edit.completer()
    comp.setCompletionPrefix("proj")
    check(comp.completionCount() == 1 and comp.currentCompletion().endswith("; "),
          "To field: typing finds a contact group")
    edit.setText(comp.pathFromIndex(comp.completionModel().index(0, 0)))
    check(len(edit.addresses()) == 4 and edit.addresses()[0] == "Ann <a@x>",
          "choosing a group inserts all its members after what was typed")
    comp.setCompletionPrefix("contoso")
    check(comp.completionCount() >= 1, "a company name finds its people")
    w.show_page("mail")


class FakeGroupServer:
    """Contact groups and photos as the server would keep them."""

    def __init__(self):
        self.groups = [{"id": g["id"], "displayName": g["displayName"], "notes": "",
                        "members": list(g["members"])} for g in _fake_contacts.contact_groups()]
        self.photos_set = []

    def install(self):
        G = graph.GraphClient
        G.contact_groups = lambda c: [dict(g) for g in self.groups]
        G.create_group = lambda c, name, notes="": self._create(name, notes)
        G.update_group = lambda c, gid, name, notes: self._group(gid).update(
            displayName=name, notes=notes)
        G.delete_group = lambda c, gid: self.groups.remove(self._group(gid))
        G.add_group_members = lambda c, gid, ms: self._group(gid)["members"].extend(
            {"id": m["key"], "displayName": m["displayName"], "type": m["type"]} for m in ms)
        G.remove_group_members = lambda c, gid, ms: self._group(gid).update(members=[
            m for m in self._group(gid)["members"]
            if m["id"] not in {x.get("key") for x in ms}])
        G.photo = lambda c, cid, folder=None: self._photo(cid)
        G.set_contact_photo = lambda c, cid, data, folder=None: self.photos_set.append(cid)

    def _create(self, name, notes):
        g = {"id": f"g{len(self.groups) + 1}x", "displayName": name, "notes": notes,
             "members": []}
        self.groups.append(g)
        return {"id": g["id"]}

    def _group(self, gid):
        return next(g for g in self.groups if g["id"] == gid)

    @staticmethod
    def _photo(cid):
        if cid != "c1":
            return None
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        img = QImage(40, 40, QImage.Format.Format_RGB32)
        img.fill(QColor("#336699"))
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(data)


def contact_edit_checks():
    import json as _json
    from waddlemail.contacts import suggestions
    from waddlemail.ui.contact_dialogs import ContactDialog, GroupDialog
    server = FakeGroupServer()
    server.install()
    view = w.contacts_view
    w.show_page("contacts")
    view.show_source("all")
    view.select_contact("c1")
    wait(500)
    check(st.photo("c1") and st.photo("c1")[0], "a contact's photo is fetched and cached")
    view.show_source("directory")
    wait(500)
    check(st.photo("dir:u1") is not None and st.photo("dir:u1")[0] is None,
          "no photo is remembered too (not asked again for a week)")

    w.save_group("", "Book club", "Monthly", [
        {"displayName": "Bo Chan", "key": "bo@y.org", "type": "contact"}], [])
    wait(400)
    club = next((g for g in st.groups() if g["name"] == "Book club"), None)
    check(club is not None and "bo@y.org" in club["members"]
          and view.selected_keys() == [("group", club["id"])],
          "new contact group created with a member, and shown")

    dlg = GroupDialog(suggestions(st), group=club)
    dlg.add_edit.setText("ana@x.com; Mom Lee <mom@home.net>")
    dlg._add_typed()
    dlg.members.item(0).setSelected(True)  # Bo
    dlg._remove_selected()
    added, removed = dlg.changes()
    check([m[1] for m in added] == ["ana@x.com", "mom@home.net"]
          and [m[1] for m in removed] == ["bo@y.org"]
          and added[0][2] == "directory" and added[1][2] == "personal",
          "group editor: typed members added (knowing where they came from), one removed")
    from waddlemail.contacts import member_payload
    w.save_group(club["id"], "Book club", "Monthly",
                 [member_payload(m[0], m[1], m[2]) for m in added], removed)
    wait(400)
    members = [m[1] for m in _json.loads(st.group_row(club["id"])["members"])]
    check(sorted(members) == ["ana@x.com", "mom@home.net"], "group members changed on the server")

    view.add_to_group(club["id"], [("Priya Shah", "priya@x.com", "personal"),
                                    ("Ana Ruiz", "ana@x.com", "directory")])  # Ana: already in
    wait(400)
    members = [m[1] for m in _json.loads(st.group_row(club["id"])["members"])]
    check(sorted(members) == ["ana@x.com", "mom@home.net", "priya@x.com"],
          "Add to group adds only who isn't in it yet")

    w.save_group(club["id"], "Readers", "", [], [])
    wait(400)
    check(st.group_row(club["id"])["name"] == "Readers", "group renamed")
    w.delete_group(club["id"])
    wait(400)
    check(st.group_row(club["id"]) is None and all(g["id"] != club["id"] for g in server.groups),
          "group deleted")

    row = st.contact_row("c1")
    dlg = ContactDialog([("", "Contacts")], row=row)
    dlg.has_birthday.setChecked(True)
    from PySide6.QtCore import QDate
    dlg.birthday.setDate(QDate(1985, 7, 4))
    dlg.home_address.city.setText("Springfield")
    dlg.website.setText("priya.example")
    v = dlg.values()
    check(v["birthday"] == "1985-07-04T11:59:00Z" and v["homeAddress"]["city"] == "Springfield"
          and v["businessHomePage"] == "priya.example" and v["mobilePhone"] == "+1 555 0199",
          "contact editor: birthday, address, website (and existing fields kept)")
    w.save_contact(v, "c1", "", "Contacts", b"\xff\xd8JPEG")
    wait(400)
    check("c1" in server.photos_set and st.photo("c1")[0] == b"\xff\xd8JPEG"
          and st.contact_row("c1")["website"] == "priya.example",
          "contact saved with its new photo")
    w.show_page("mail")


class FakeCalendarServer:
    def __init__(self):
        today = dt.date.today()
        self.monday = today - dt.timedelta(days=today.weekday())
        at = lambda d, h: utc(dt.datetime.combine(self.monday + dt.timedelta(days=d),  # noqa
                                                  dt.time(h)).astimezone())
        self.events = {
            "standup": {"id": "standup", "subject": "Standup", "isOrganizer": True,
                        "start": {"dateTime": at(0, 9)}, "end": {"dateTime": at(0, 10)},
                        "showAs": "busy", "categories": ["Blue category"]},
            "review": {"id": "review", "subject": "Design review", "isOrganizer": False,
                       "start": {"dateTime": at(1, 14)}, "end": {"dateTime": at(1, 15)},
                       "organizer": {"emailAddress": {"name": "Ana Ruiz", "address": "ana@x.com"}},
                       "attendees": [{"emailAddress": {"name": "Me", "address": "jordan@contoso.com"},
                                      "type": "required", "status": {"response": "none"}}],
                       "responseStatus": {"response": "notResponded"},
                       "isOnlineMeeting": True,
                       "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"}},
        }
        self.saved, self.responses, self.schedules = [], [], []
        self.reminder_calls, self.deleted = [], []
        # a weekly series on Thursdays at 16:00, two occurrences in view
        self.master = {"id": "series1", "subject": "Weekly 1:1", "isOrganizer": True,
                       "start": {"dateTime": at(4, 16)}, "end": {"dateTime": at(4, 17)},
                       "showAs": "busy", "type": "seriesMaster",
                       "recurrence": {"pattern": {"type": "weekly", "interval": 1,
                                                  "daysOfWeek": [
                                                      (self.monday + dt.timedelta(days=4))
                                                      .strftime("%A").lower()],
                                                  "firstDayOfWeek": "sunday"},
                                      "range": {"type": "noEnd",
                                                "startDate": (self.monday + dt.timedelta(
                                                    days=4)).isoformat()}}}
        for k, d in (("occ1", 4), ("occ2", 11)):
            self.events[k] = {"id": k, "subject": "Weekly 1:1", "isOrganizer": True,
                              "start": {"dateTime": at(d, 16)}, "end": {"dateTime": at(d, 17)},
                              "showAs": "busy", "type": "occurrence", "seriesMasterId": "series1"}

    def install(self):
        G = graph.GraphClient
        G.calendars = lambda c: [{"id": "cal", "name": "Calendar", "isDefaultCalendar": True,
                                  "hexColor": "#2f9e44",
                                  "allowedOnlineMeetingProviders": ["teamsForBusiness"]}]
        G.categories = lambda c: [{"displayName": "Blue category", "color": "preset7"},
                                  {"displayName": "Travel", "color": "preset4"}]
        G.calendar_view = lambda c, cid, start, end: [
            dict(e, start={"dateTime": e["start"]["dateTime"], "timeZone": "UTC"},
                 end={"dateTime": e["end"]["dateTime"], "timeZone": "UTC"})
            for e in self.events.values()
            if e["start"]["dateTime"] < end and e["end"]["dateTime"] > start]
        G.save_event = lambda c, fields, eid=None, cid=None: self._save(fields, eid)
        G.delete_event = lambda c, eid: (self.deleted.append(eid), self.events.pop(eid, None))
        G.respond_event = lambda c, eid, resp, comment="": self.responses.append((eid, resp))
        G.event_body = lambda c, eid: ("text", "Full agenda")
        G.schedule = lambda c, addresses, start, end, interval=30: self._schedule(addresses)
        G.get_event = lambda c, eid: dict(self.master, id=eid)
        G.dismiss_reminder = lambda c, eid: self.reminder_calls.append(("dismiss", eid))
        G.snooze_reminder = lambda c, eid, until: self.reminder_calls.append(("snooze", eid))

    def _save(self, fields, eid):
        self.saved.append(fields)
        item = dict(self.events.get(eid, {}), **fields, id=eid or f"new{len(self.saved)}",
                    isOrganizer=True)
        if fields.get("isOnlineMeeting"):
            item["onlineMeeting"] = {"joinUrl": "https://teams.microsoft.com/l/meetup-join/new"}
        self.events[item["id"]] = item
        return item

    def _schedule(self, addresses):
        self.schedules.append(addresses)
        nine = dt.datetime.combine(self.monday + dt.timedelta(days=7), dt.time(9)).astimezone()
        return [{"scheduleId": a, "scheduleItems": [
            {"status": "busy", "start": {"dateTime": utc(nine)},
             "end": {"dateTime": utc(nine + dt.timedelta(hours=2))}}] if a == "ana@x.com"
            else []} for a in addresses]


def utc(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def calendar_checks():
    from PySide6.QtCore import QPointF
    from waddlemail.ui.calendar_view import MONTH, DAY, WEEK
    global _calendar_server
    server = _calendar_server = FakeCalendarServer()
    server.install()
    page = w.calendar_page
    w.setFocus()
    QTest.keyClick(w, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    page.go_to(server.monday)
    page.set_mode(WEEK)
    w.refresh_calendar(calendars=True)
    wait(600)
    check(w.pages.currentWidget() is page and page.cal_list.count() == 1,
          "Ctrl+2 opens the Calendar with its calendars")
    check(sorted(e.subject for e in page.grid.events) == ["Design review", "Standup",
                                                           "Weekly 1:1"],
          "the week's events are shown")
    standup = next(e for e in page.grid.events if e.id == "standup")
    check(standup.color.name() == "#4f8cff", "an event takes its category's colour")
    page.select_event("review")
    check(page.accept_b.isVisible() and page.join.isVisible()
          and "Join Teams meeting" in page.details.toHtml(),
          "an invitation offers Accept / Tentative / Decline and the Teams link")
    w.respond_event("review", "tentativelyAccept")
    wait(400)
    check(server.responses == [("review", "tentativelyAccept")], "responding to an invitation")
    page.set_mode(MONTH)
    check(page.stack.currentWidget() is page.month_grid and len(page.days()) == 42,
          "month view")
    page.set_mode(DAY)
    check(page.days() == [server.monday], "day view")
    page.set_mode(WEEK)
    wait(400)

    asked = []
    real_new = w.new_event
    w.new_event = lambda s=None, e=None, all_day=False: asked.append((s, e, all_day))
    x = page.grid.GUTTER + page.grid._col_w() * 2.5  # the third day (the week may start Sunday)
    y = page.grid.HOUR_H * 13 + 5                    # 13:00
    QTest.mouseClick(page.grid, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPointF(x, y).toPoint())
    QTest.keyClick(page.grid, Qt.Key.Key_Return)
    w.new_event = real_new
    check(asked and asked[0][0].hour == 13 and asked[0][0].date() == page.grid.days[2]
          and (asked[0][1] - asked[0][0]) == dt.timedelta(minutes=30),
          "pick a time in the grid, Enter: new event there")

    nine = dt.datetime.combine(server.monday + dt.timedelta(days=7), dt.time(9)).astimezone()
    dlg = w._event_editor(start=nine, end=nine + dt.timedelta(hours=1))
    dlg.title.setText("Budget sync")
    dlg.required.setText("Ana Ruiz <ana@x.com>; ")
    dlg.teams.setChecked(True)
    dlg.show_as.setCurrentIndex(dlg.show_as.findData("workingElsewhere"))
    dlg._category_actions["Travel"].setChecked(True)
    dlg.tabs.setCurrentIndex(1)
    wait(1200)  # (the editor waits half a second before asking)
    check(server.schedules and "ana@x.com" in server.schedules[-1]
          and "Not free then: Ana Ruiz" in dlg.sched_status.text(),
          "scheduling: everyone's availability, and who isn't free")
    dlg.find_free_time()
    check(dlg._start() == nine + dt.timedelta(hours=2), "find next free time: after Ana's 9-11")
    check(dlg.save_button.text() == "Send", "with attendees it's a meeting: Send")
    w._save_event(dlg, None)
    wait(400)
    f = server.saved[-1]
    check(f["subject"] == "Budget sync" and f["isOnlineMeeting"]
          and f["onlineMeetingProvider"] == "teamsForBusiness"
          and f["showAs"] == "workingElsewhere" and f["categories"] == ["Travel"]
          and f["attendees"][0]["emailAddress"]["address"] == "ana@x.com",
          "meeting saved: attendees, Teams, show as, category")
    new_id = page.selected
    row = st.event_row(new_id) if new_id else None
    check(row is not None and row["online_url"].endswith("/new"),
          "the new meeting comes back with its Teams link")

    dlg = w._event_editor(start=nine, end=nine)
    dlg.title.setText("Conference")
    dlg.all_day.setChecked(True)
    check(dlg.show_as.currentData() == "free", "all-day events default to Free")
    w._save_event(dlg, None)
    wait(400)
    f = server.saved[-1]
    day = (server.monday + dt.timedelta(days=7)).isoformat()
    nxt = (server.monday + dt.timedelta(days=8)).isoformat()
    check(f["isAllDay"] and f["start"]["dateTime"] == f"{day}T00:00:00"
          and f["end"]["dateTime"] == f"{nxt}T00:00:00", "all-day event: whole days")

    dlg = w._event_editor(row=st.event_row("standup"))
    wait(300)
    check(dlg.notes.toPlainText() == "Full agenda", "editing loads the full notes")
    dlg.title.setText("Daily standup")
    w._save_event(dlg, st.event_row("standup"))
    wait(400)
    check(server.saved[-1]["subject"] == "Daily standup" and "body" not in server.saved[-1],
          "editing sends the change (and leaves the notes alone)")
    w.delete_event("standup")
    wait(400)
    check("standup" not in server.events and st.event_row("standup") is None,
          "delete (a meeting you organised: cancel)")
    w.show_page("mail")


def recurrence_checks():
    from waddlemail.calendars import event_row as cal_row
    from waddlemail.ui.recurrence_dialog import RecurrenceDialog
    server = _calendar_server
    w.show_page("calendar")
    nine = dt.datetime.combine(server.monday + dt.timedelta(days=14), dt.time(9)).astimezone()
    dlg = w._event_editor(start=nine, end=nine + dt.timedelta(hours=1))
    dlg.title.setText("Team sync")
    labels = [dlg.repeat.itemText(i) for i in range(dlg.repeat.count())]
    weekday = nine.strftime("%A")
    check(labels[0] == "Does not repeat" and f"Weekly on {weekday}" in labels
          and labels[-1] == "Custom…", "Repeat offers Outlook's choices for the date")
    dlg.repeat.setCurrentIndex(labels.index(f"Weekly on {weekday}"))
    asked_before = len(server.saved)
    w._save_event(dlg, None)
    wait(500)
    f = server.saved[-1] if len(server.saved) > asked_before else {}
    rec = f.get("recurrence") or {}
    check(rec.get("pattern", {}).get("daysOfWeek") == [weekday.lower()]
          and rec["range"]["startDate"] == nine.date().isoformat()
          and f["start"]["timeZone"] == rec["range"]["recurrenceTimeZone"] != "",
          "a weekly series is sent with its pattern, in your time zone")

    rd = RecurrenceDialog(nine.date())
    rd.kind.setCurrentIndex(2)          # monthly
    rd.m_on_nth.setChecked(True)
    rd.interval.setValue(2)
    rd.end_after.setChecked(True)
    rd.count.setValue(6)
    rec = rd.recurrence()
    check(rec["pattern"]["type"] == "relativeMonthly" and rec["pattern"]["interval"] == 2
          and rec["range"]["numberOfOccurrences"] == 6
          and rd.summary.text().startswith("Every 2 months on the"),
          "custom repeat: every 2 months on the Nth weekday, 6 times")

    master = cal_row(dict(server.master, start={"dateTime": server.master["start"]["dateTime"],
                                                "timeZone": "UTC"},
                          end={"dateTime": server.master["end"]["dateTime"], "timeZone": "UTC"}),
                     "a", "cal")
    dlg = w._event_editor(row=master, recurrence=server.master["recurrence"], series=True)
    check(dlg.repeat.currentText().startswith("Weekly on") and dlg.windowTitle() ==
          "Edit event series", "editing a series shows its pattern")
    dlg.repeat.setCurrentIndex(0)
    f = dlg.draft().fields()
    check("recurrence" in f and f["recurrence"] is None, "...and can stop it repeating")
    w.refresh_calendar()
    wait(500)
    occ = st.event_row("occ1")
    dlg = w._event_editor(row=occ)
    check(occ is not None and not dlg.repeat.isEnabled() and dlg.windowTitle() ==
          "Edit occurrence", "one occurrence on its own can't change the pattern")
    w._series_choice = lambda row, verb: "series"
    w.delete_event("occ1")
    wait(500)
    check(server.deleted[-1] == "series1", "deleting the whole series deletes its master")
    del w._series_choice


def reminder_checks():
    server = _calendar_server
    soon = dt.datetime.now().astimezone() + dt.timedelta(minutes=10)
    for k, minutes in (("rem1", 10), ("rem2", 12)):
        t = dt.datetime.now().astimezone() + dt.timedelta(minutes=minutes)
        server.events[k] = {"id": k, "subject": f"Call {k}", "isOrganizer": True,
                            "showAs": "busy", "isReminderOn": True,
                            "reminderMinutesBeforeStart": 15,
                            "start": {"dateTime": utc(t)},
                            "end": {"dateTime": utc(t + dt.timedelta(minutes=30))}}
    w.notifier.sent.clear()
    w.refresh_calendar()
    wait(600)
    rw = w.reminder_window
    check(rw.isVisible() and rw.list.topLevelItemCount() == 2
          and "in 10 min" in rw.list.topLevelItem(0).text(1),
          "reminders window: what is due, soonest first")
    check(getattr(w.notifier, "kinds", [])[-2:] == ["reminder", "reminder"],
          "a notification per reminder")
    first = rw.list.topLevelItem(0)
    rw.list.clearSelection()
    first.setSelected(True)
    rw.snooze_for.setCurrentIndex(0)  # 5 minutes
    rw.snooze.click()
    wait(300)
    check(rw.list.topLevelItemCount() == 1 and ("snooze", "rem1") in server.reminder_calls,
          "snooze: gone for now, and Exchange is told")
    rw.dismissRequested.emit(rw.all_keys())
    wait(300)
    check(not rw.isVisible() and ("dismiss", "rem2") in server.reminder_calls,
          "dismiss all: the window goes away")
    w.reminders.check(soon + dt.timedelta(minutes=1))  # 6 minutes on: rem1's snooze is over
    check(rw.isVisible() and rw.list.topLevelItemCount() == 1, "a snoozed reminder comes back")
    rw.dismissRequested.emit(rw.all_keys())
    w.show_page("mail")


def new_button_checks():
    calls = []
    real = {name: getattr(w, name) for name in ("new_mail", "new_event", "new_contact")}
    for name in real:
        setattr(w, name, lambda name=name: calls.append(name))
    seen = []
    for page in ("mail", "calendar", "contacts"):
        w.show_page(page)
        w.new_btn.click()
        seen.append(w.new_btn.text())
    check(seen == ["New Mail", "New Event", "New Contact"]
          and calls == ["new_mail", "new_event", "new_contact"],
          "the New button makes what belongs to the page you're on")
    menu = [a.text() for a in w.new_btn.menu().actions() if not a.isSeparator()]
    check(menu[:3] == ["&Mail message", "&Event", "&Contact…"],
          "its arrow still offers mail, event and contact from anywhere")
    for name, fn in real.items():
        setattr(w, name, fn)
    w.show_page("mail")


class FakeOutbox:
    def __init__(self):
        self.sent, self.drafts, self.updates, self.sent_drafts, self.responses = [], [], [], [], []

    def install(self):
        G = graph.GraphClient
        G.send_mail = lambda c, message: self.sent.append(message)
        G.create_draft = lambda c, message: (self.drafts.append(message),
                                             {"id": f"draft{len(self.drafts)}"})[1]
        G.create_response = lambda c, mid, kind: (self.responses.append((mid, kind)),
                                                  {"id": f"resp{len(self.responses)}"})[1]
        G.update_message = lambda c, mid, changes: self.updates.append((mid, changes))
        G.send_draft = lambda c, did: self.sent_drafts.append(did)


def compose_windows():
    from waddlemail.ui.compose_window import ComposeWindow
    return [x for x in w._windows if isinstance(x, ComposeWindow)]


def compose_checks():
    from PySide6.QtGui import QTextCursor
    from waddlemail.ui.preferences import PreferencesDialog
    out = FakeOutbox()
    out.install()
    st.set_signature("a", "<p><b>Jordan Lee</b><br>Contoso</p>", True, True)

    w.new_mail(to=["Ana Ruiz <ana@x.com>"])
    wait(200)
    win = compose_windows()[-1]
    body = win.editor.text()
    check(win.to.text().startswith("Ana Ruiz <ana@x.com>") and "Jordan Lee" in body
          and win.editor.edit.textCursor().position() == 0,
          "new mail: To filled, signature added, cursor above it")
    check(not win.cc.isVisible() and not win.bcc.isVisible(), "Cc and Bcc hidden until asked")
    win.act_bcc.trigger()
    check(win.bcc.isVisible(), "Bcc shown on demand")
    win.subject.setText("Hello")
    win.editor.edit.textCursor().insertText("Hi Ana,")
    cur = win.editor.edit.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    win.editor.edit.setTextCursor(cur)
    win.editor.act_bold.trigger()
    html = win.editor.html()
    check("font-weight:700" in html or "font-weight:600" in html, "formatting: bold")
    win.draftRequested.emit(win)
    wait(300)
    check(out.drafts and win.draft_id == "draft1", "Save draft: kept in Drafts")
    win.act_send.trigger()
    wait(400)
    check(out.sent_drafts == ["draft1"] and out.updates[-1][0] == "draft1"
          and out.updates[-1][1]["body"]["contentType"] == "html"
          and "Hi Ana," in out.updates[-1][1]["body"]["content"]
          and win not in compose_windows(), "Send: the saved draft goes out; the window closes")

    w.show_page("mail")
    w.folder_tree.select_folder("in")
    wait(300)
    w.respond("reply", "m1")
    wait(400)
    win = compose_windows()[-1]
    text = win.editor.text()
    order = [text.find(x) for x in ("Jordan Lee", "From: Priya Shah", "Final numbers")]
    check(win.subject.text() == "RE: Q4 budget review" and "Priya" in win.to.text()
          and -1 not in order and order == sorted(order)
          and win.editor.edit.textCursor().position() == 0,
          "reply: cursor at the top, then signature, then Outlook's header and the original")
    win.insert_signature()
    text = win.editor.text()
    check(text.count("Jordan Lee") == 2 and text.rfind("Jordan Lee") < text.find("From: Priya"),
          "Insert signature: right above the quoted text")
    win.editor.edit.textCursor().insertText("Thanks!")
    win.act_plain.setChecked(True)  # (the "lose formatting?" question is answered Yes)
    check(win.editor.is_plain() and not win.editor.bar.isVisible()
          and win.outgoing().html is False, "plain text: no formatting bar, sent as text")
    win.act_send.trigger()
    wait(400)
    check(st.message_row("m1")["last_verb"] == 102,
          "the original is marked replied as soon as the reply is sent")
    check(out.responses[-1] == ("m1", "reply") and out.sent_drafts[-1] == "resp1"
          and out.updates[-1][1]["body"]["content"].startswith("Thanks!"),
          "reply sent through Exchange's reply draft (keeps the thread)")

    w.respond("forward", "m1")
    wait(400)
    win = compose_windows()[-1]
    check(win.subject.text() == "FW: Q4 budget review" and not win.to.text().strip(),
          "forward: FW: subject, nobody in To")
    win.sent = True
    win.close()

    st.set_signature("a", "<p>Sig</p>", False, False)
    w.new_mail()
    wait(200)
    win = compose_windows()[-1]
    check("Sig" not in win.editor.text(), "the signature can be left off new messages")
    win.sent = True
    win.close()

    dlg = PreferencesDialog(w.settings, lambda: None, w, store=st,
                            accounts=[("a", "jordan@contoso.com")])
    dlg.sig_editor.edit.setHtml("<p><i>Best,</i><br>Jordan</p>")
    dlg.sig_new.setChecked(True)
    dlg.sig_reply.setChecked(False)
    dlg.font_size.setValue(12)
    dlg.text_format.setChecked(True)
    dlg._save()
    sig = st.signature("a")
    check(w.settings.get("compose/font") == "Calibri",
          "an uninstalled default font (Calibri) isn't replaced by its stand-in")
    check("Jordan" in sig["html"] and sig["on_new"] == 1 and sig["on_reply"] == 0
          and w.settings.get("compose/size") == 12 and not w.settings.get("compose/html"),
          "Preferences: signature per account, when to add it, font and format")
    w.settings.set("compose/html", True)


def travel_id():
    return folder_named("Travel")["id"]


QTimer.singleShot(1200, run)
sys.exit(app.exec())
