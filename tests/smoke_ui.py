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
    for x in list(w._windows):  # (offscreen it would stay the active window, and its
        x.close()               # shortcuts, Del included, would catch keys sent elsewhere)
    wait(200)

    folder_checks()
    move_checks()
    delete_checks()
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
    attachment_checks()
    picture_checks()
    outbox_checks()
    tasks_checks()

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


def delete_checks():
    pane = w.folder_tree
    w._sync_all_folders()
    w._populate_folders()
    deletes, batch = [], []
    graph.GraphClient.delete_message = lambda c, mid: deletes.append(mid)
    pane.select_folder("in")
    wait(300)
    click("m3")
    asked = []
    QMessageBox.question = staticmethod(
        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    w.act_delete.trigger()
    check(st.message_row("m3")["folder_id"] == "del" and "m3" not in ids() and not asked,
          "Delete moves the message to Deleted Items (no question)")
    check(view.current_id() not in (None, "m3"), "...and the next message is selected")
    wait(2000)
    check(("m3", "del") in server_moves, "...through the change queue")

    pane.select_folder("del")
    wait(300)
    click("m3")
    w.act_delete.trigger()
    check(asked and "Permanently delete 1 message" in asked[-1]
          and st.message_row("m3")["folder_id"] == "" and "m3" not in ids(),
          "in Deleted Items, Delete deletes for good (after asking)")
    wait(2000)
    check(deletes == ["m3"] and st.message_row("m3") is None,
          "...sent to the server, then forgotten here")

    st.apply_message_changes("in", [m("m7", "Lunch?", "Sam Lee", 2)])
    pane.select_folder("in")
    wait(300)
    click("m7")
    w.act_delete_forever.trigger()
    check(len(asked) == 2 and st.message_row("m7")["folder_id"] == "",
          "Shift+Del deletes for good from any folder")
    wait(2000)

    # Empty Deleted Items: everything on the server, and its folders
    old = server.create(None, "Old stuff", "del")["id"]
    w._sync_all_folders()
    w._populate_folders()
    st.apply_message_changes("del", [m("d1", "Spam", "Someone", 5),
                                     m("d2", "More", "Someone", 6, read=True)])
    st.apply_message_changes(old, [m("d3", "Ancient", "Someone", 900)])
    graph.GraphClient.folder_message_ids = lambda c, fid: ["d1", "d2", "d9"]  # (d9: not here)
    graph.GraphClient.delete_messages = lambda c, mids, progress=None: (
        batch.extend(mids), progress and progress(len(mids), len(mids)), [])[2]
    pane.select_folder(old)
    wait(300)
    w.empty_deleted_items("del")
    wait(1500)
    check(batch == ["d1", "d2", "d9"] and old not in server.folders
          and all(st.message_row(i) is None for i in ("d1", "d2", "d3"))
          and st.folder_row(old) is None,
          "Empty Deleted Items deletes all its messages (even ones not kept here) and folders")
    check(w._current_folder == "del", "...and moves out of a folder that's gone")
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


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
    check(dlg.notes.text() == "Full agenda", "editing loads the full notes")
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
        G.sent_with_mark = lambda c, mark: any(
            p["value"] == mark for m in self.sent
            for p in m.get("singleValueExtendedProperties", []))


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


class FakeAttachments:
    """attachments per item path, as the server keeps them."""

    def __init__(self):
        self.items: dict[str, list[dict]] = {}
        self.added, self.deleted = [], []
        self.n = 0

    def install(self):
        G = graph.GraphClient
        G.attachments = lambda c, item: list(self.items.get(item, []))
        G.add_attachment = lambda c, item, name, data, ctype="", content_id=None: self._add(
            item, name, data, content_id)
        G.delete_attachment = lambda c, item, aid: self._delete(item, aid)
        G.attachment_content = lambda c, item, aid: f"content of {aid}".encode()

    def _add(self, item, name, data, content_id=None):
        self.n += 1
        self.items.setdefault(item, []).append({"id": f"att{self.n}", "name": name,
                                                "size": len(data),
                                                "isInline": content_id is not None})
        self.added.append((item, name))
        self.inline = getattr(self, "inline", []) + ([(name, content_id)] if content_id else [])
        return f"att{self.n}"

    def _delete(self, item, aid):
        self.deleted.append((item, aid))
        self.items[item] = [a for a in self.items.get(item, []) if a["id"] != aid]


def attachment_checks():
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent
    out, atts = FakeOutbox(), FakeAttachments()
    out.install()
    atts.install()
    folder = tmp / "files"
    folder.mkdir(exist_ok=True)
    (folder / "plan.pdf").write_bytes(b"%PDF" + b"x" * 3000)
    (folder / "notes.txt").write_text("remember the milk")

    w.new_mail(to=["Ana Ruiz <ana@x.com>"])
    wait(200)
    win = compose_windows()[-1]
    win.subject.setText("Files")
    win.attachments.add_files([str(folder / "plan.pdf")])
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(folder / "notes.txt"))])
    vp = win.editor.edit.viewport()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win.eventFilter(vp, drop)
    names = [a.name for a in win.attachments.items()]
    check(names == ["plan.pdf", "notes.txt"] and "notes.txt" not in win.editor.text()
          and "2 files" in win.attachments.total.text(),
          "attach: button and dropping a file on the body (not pasted as text)")
    win.act_send.trigger()
    wait(500)
    check(all(m.get("subject") != "Files" for m in out.sent),
          "mail with attachments isn't sent directly (sendMail can't carry large files)")
    check(out.drafts and [n for i, n in atts.added] == ["plan.pdf", "notes.txt"]
          and out.sent_drafts[-1] == f"draft{len(out.drafts)}",
          "mail with attachments goes out as a draft with its files")

    # a forward: the original has two attachments; take one out
    st.conn.execute("UPDATE messages SET has_attachments=1 WHERE id='m1'")
    st.conn.commit()
    atts.items["/me/messages/m1"] = [{"id": "o1", "name": "budget.xlsx", "size": 100},
                                     {"id": "o2", "name": "old.doc", "size": 50}]
    real_create = graph.GraphClient.create_response

    def create_forward(c, mid, kind):  # Exchange copies the originals onto the forward
        made = real_create(c, mid, kind)
        atts.items[f"/me/messages/{made['id']}"] = [
            {"id": "f1", "name": "budget.xlsx", "size": 100},
            {"id": "f2", "name": "old.doc", "size": 50}]
        return made

    graph.GraphClient.create_response = create_forward
    w.respond("forward", "m1")
    wait(500)
    win = compose_windows()[-1]
    check([a.name for a in win.attachments.items()] == ["budget.xlsx", "old.doc"],
          "a forward lists the original's attachments")
    win.attachments.list.item(1).setSelected(True)
    win.attachments.remove_selected()
    win.to.setText("Bo <bo@y.org>; ")
    win.act_send.trigger()
    wait(500)
    draft = out.sent_drafts[-1]
    check([aid for item, aid in atts.deleted] == ["f2"]
          and [a["name"] for a in atts.items[f"/me/messages/{draft}"]] == ["budget.xlsx"],
          "...and one taken out isn't forwarded")
    graph.GraphClient.create_response = real_create

    # received attachments: listed under the header, opened, saved
    from waddlemail.ui import main_window as mw
    w._show_in_pane("m1")
    wait(400)
    strip = w.msg_view.attachments
    check(not strip.isHidden() and [a["name"] for a in strip.attachments()]
          == ["budget.xlsx", "old.doc"], "a received message's attachments show in the reader")
    opened, real_desktop, real_dialog = [], mw.QDesktopServices, mw.QFileDialog
    mw.QDesktopServices = type("D", (), {"openUrl": staticmethod(
        lambda url: opened.append(url.toLocalFile()))})
    strip.open_requested.emit("m1", strip.attachments()[:1])
    wait(400)
    check(len(opened) == 1 and opened[0].endswith("/budget.xlsx")
          and Path(opened[0]).read_bytes() == b"content of o1",
          "...opening one downloads it and hands it to the desktop")
    saved = tmp / "saved"
    saved.mkdir(exist_ok=True)
    mw.QFileDialog = type("F", (), {"getExistingDirectory": staticmethod(
        lambda *a: str(saved))})
    for _ in range(2):
        strip.save_requested.emit("m1", strip.attachments())
        wait(400)
    check(sorted(f.name for f in saved.iterdir()) == ["budget (2).xlsx", "budget.xlsx",
                                                     "old (2).doc", "old.doc"],
          "...and Save all writes them to a folder, not over files already there")
    mw.QDesktopServices, mw.QFileDialog = real_desktop, real_dialog

    # an event with an attachment, as a meeting
    server = _calendar_server
    nine = dt.datetime.combine(server.monday + dt.timedelta(days=21), dt.time(9)).astimezone()
    dlg = w._event_editor(start=nine, end=nine + dt.timedelta(hours=1))
    dlg.title.setText("Review the plan")
    dlg.required.setText("Ana Ruiz <ana@x.com>; ")
    dlg.attachments.add_files([str(folder / "plan.pdf")])
    saves_before = len(server.saved)
    w._save_event(dlg, None)
    wait(500)
    event_items = [i for i, n in atts.added if i.startswith("/me/events/")]
    check(event_items and atts.added[-1][1] == "plan.pdf"
          and len(server.saved) == saves_before + 2 and "attendees" not in server.saved[-2]
          and list(server.saved[-1]) == ["attendees"],
          "event attachment uploaded before the attendees are invited (one invitation)")
    eid = event_items[-1].rsplit("/", 1)[1]
    dlg = w._event_editor(row=st.event_row(eid))
    wait(400)
    check([a.name for a in dlg.attachments.items()] == ["plan.pdf"]
          and not dlg.attachments_changed, "editing an event shows its attachments")
    dlg.attachments.list.item(0).setSelected(True)
    dlg.attachments.remove_selected()
    w._save_event(dlg, st.event_row(eid))
    wait(500)
    check(atts.items.get(f"/me/events/{eid}") == [], "...and one can be removed")


def picture_checks():
    from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QUrl
    from PySide6.QtGui import QDropEvent, QImage, QMouseEvent
    out, atts = FakeOutbox(), FakeAttachments()
    out.install()
    atts.install()
    pic = tmp / "files" / "chart.png"
    img = QImage(900, 300, QImage.Format.Format_RGB32)
    img.fill(0x3366CC)
    img.save(str(pic))

    w.new_mail(to=["Ana Ruiz <ana@x.com>"])
    wait(200)
    win = compose_windows()[-1]
    win.subject.setText("Chart")
    win.editor.insert_picture_file(str(pic))
    html = win.editor.html()
    check("data:image/png;base64," in html and 'width="640"' in html,
          "insert picture: in the body, scaled to fit")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(pic))])
    vp = win.editor.edit.viewport()
    win.eventFilter(vp, QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                                   Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    check(win.editor.html().count("data:image/png") == 2 and not win.attachments.items(),
          "a picture dropped on the body goes into the text, not the attachments")
    # resizing: click the picture, drag its bottom-right corner left
    e = win.editor.edit
    pos = next(i for i in range(e.document().characterCount()) if e.picture_format(i))
    rect = e.picture_rect(pos)

    def mouse(kind, pt, buttons=Qt.MouseButton.LeftButton):
        QApplication.sendEvent(e.viewport(), QMouseEvent(
            kind, QPointF(pt), QPointF(e.viewport().mapToGlobal(pt)),
            Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else
            Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier))

    mouse(QEvent.Type.MouseButtonPress, rect.center())
    mouse(QEvent.Type.MouseButtonRelease, rect.center(), Qt.MouseButton.NoButton)
    corner = rect.bottomRight()
    mouse(QEvent.Type.MouseButtonPress, corner)
    for dx in (-100, -320):
        mouse(QEvent.Type.MouseMove, QPoint(corner.x() + dx, corner.y()))
    mouse(QEvent.Type.MouseButtonRelease, QPoint(corner.x() - 320, corner.y()),
          Qt.MouseButton.NoButton)
    fmt = e.picture_format(pos)
    check(e.picture == pos and (fmt.width(), fmt.height()) == (320, 107)
          and 'width="320" height="107"' in win.editor.html(),
          "a picture is resized by dragging its corner (proportions kept)")
    e.undo()
    check(e.picture_format(pos).width() == 640, "...and one undo puts the whole drag back")
    win.act_send.trigger()
    wait(500)
    patched = out.updates[-1][1]["body"]["content"] if out.updates else ""
    created = out.drafts[-1]["body"]["content"] if out.drafts else ""
    content_id = atts.inline[-1][1] if getattr(atts, "inline", None) else "?"
    check(not any(m.get("subject") == "Chart" for m in out.sent)
          and len(atts.inline) == 1 and f"cid:{content_id}" in created + patched
          and "data:image" not in created + patched,
          "sent as an inline attachment the body links to (one, though it's in twice)")

    # a received message's pictures (cid:) show in the reader and in a reply
    st.put_body("m2", "html", '<p>See <img src="cid:logo@x"></p>', 10 ** 9)
    graph.GraphClient.inline_images = lambda c, item, wanted: {"logo@x": ("image/png",
                                                                          pic.read_bytes())}
    w._current_message = None
    w._show_in_pane("m2")
    wait(400)
    check(w.msg_view._body and "data:image/png;base64," in w.msg_view._body[1]
          and "cid:" not in w.msg_view._body[1], "a received message's pictures show")
    w.respond("reply", "m2")
    wait(500)
    win = compose_windows()[-1]
    check("data:image/png" in win.editor.html(), "...and in the quote of a reply")
    before = len(atts.inline)
    win.act_send.trigger()
    wait(500)
    check(len(atts.inline) == before + 1 and "cid:" in out.updates[-1][1]["body"]["content"],
          "...which sends it along as an inline picture")

    # pictures in an event's notes
    server = _calendar_server
    ten = dt.datetime.combine(server.monday + dt.timedelta(days=22), dt.time(10)).astimezone()
    dlg = w._event_editor(start=ten, end=ten + dt.timedelta(hours=1))
    dlg.title.setText("Site visit")
    dlg.notes.edit.textCursor().insertText("Map: ")
    dlg.notes.insert_picture_file(str(pic))
    before = len(atts.inline)
    w._save_event(dlg, None)
    wait(500)
    body = server.saved[-1].get("body") or {}
    check(body.get("contentType") == "html" and "cid:" in body.get("content", "")
          and "data:image" not in body["content"] and len(atts.inline) == before + 1
          and atts.added[-1][0].startswith("/me/events/"),
          "event notes: a picture is saved as an inline attachment the notes link to")
    eid = atts.added[-1][0].rsplit("/", 1)[1]
    real_body = graph.GraphClient.event_body
    graph.GraphClient.event_body = lambda c, e: (
        "html", '<p>Map: <img src="cid:logo@x"></p>')
    atts.items[f"/me/events/{eid}"] = [{"id": "e1", "name": "image001.png", "isInline": True}]
    dlg = w._event_editor(row=st.event_row(eid))
    wait(400)
    check("data:image/png" in dlg.notes.html() and dlg.had_pictures,
          "...opening the event shows the picture")
    dlg.notes.edit.selectAll()
    dlg.notes.edit.textCursor().insertText("No map after all")
    w._save_event(dlg, st.event_row(eid))
    wait(500)
    check(atts.items[f"/me/events/{eid}"] == [] and "img" not in server.saved[-1]["body"]["content"],
          "...and a picture taken out of the notes is removed from the event")
    graph.GraphClient.event_body = real_body


def outbox_checks():
    import requests
    out = FakeOutbox()
    out.install()
    real_send = graph.GraphClient.send_mail

    def down(c, message):
        raise requests.ConnectionError("no route to host")

    graph.GraphClient.send_mail = down
    w.new_mail(to=["Ana Ruiz <ana@x.com>"])
    wait(200)
    win = compose_windows()[-1]
    win.subject.setText("While offline")
    win.editor.edit.textCursor().insertText("Sent from the train. ")
    win.act_send.trigger()
    wait(500)
    check(win not in compose_windows() and len(st.outbox_items()) == 1
          and "Outbox" in w.statusBar().currentMessage(),
          "offline: the message waits in the Outbox and the window closes")
    w.folder_tree.select_folder("out")
    wait(300)
    item = w.folder_tree._find("out")
    check(item.data(0, Qt.ItemDataRole.UserRole + 2) == 1, "the Outbox folder counts it")
    waiting = [i for i in ids() if i.startswith("outbox:")]
    check(len(waiting) == 1, "...and lists it")
    w._show_in_pane(waiting[0])
    wait(300)
    check(w.msg_view._body and "train" in w.msg_view._body[1]
          and "While offline" in w.msg_view.header.subject.text(),
          "...and shows it in the reader")

    graph.GraphClient.send_mail = real_send
    w.flush_outbox()
    wait(800)
    check(not st.outbox_items() and any(m["subject"] == "While offline" for m in out.sent)
          and not [i for i in ids() if i.startswith("outbox:")],
          "back online: it's sent and leaves the Outbox")

    # the server refuses one: it stays, reported, and can be edited or deleted
    def refuse(c, message):
        raise graph.GraphError("Graph 400: invalid recipient", 400)

    graph.GraphClient.send_mail = down
    w.new_mail(to=["nobody"])
    wait(200)
    win = compose_windows()[-1]
    win.to.setText("Bo <bo@y.org>; ")
    win.cc.setText("Cy <cy@z.org>; ")
    win.subject.setText("Typo")
    win.act_send.trigger()
    wait(500)
    graph.GraphClient.send_mail = refuse
    before = len(warnings)
    w.flush_outbox()
    wait(800)
    [item] = st.outbox_items()
    check(item["refused"] and len(warnings) == before + 1 and "Typo" in warnings[-1],
          "a refused message stays in the Outbox, and you're told")
    w.flush_outbox()
    wait(500)
    check(len(warnings) == before + 1, "...and isn't retried by itself")
    w._refresh_messages()
    waiting = [i for i in ids() if i.startswith("outbox:")]
    w.open_message(waiting[0])
    wait(300)
    win = compose_windows()[-1]
    check(win.subject.text() == "Typo" and "cy@z.org" in win.cc.text()
          and win.cc.isVisible() and not st.outbox_items(),
          "opening it takes it out of the Outbox to edit")
    graph.GraphClient.send_mail = down
    win.act_send.trigger()
    wait(500)
    w._refresh_messages()
    waiting = [i for i in ids() if i.startswith("outbox:")]
    check(len(waiting) == 1 and w.delete_messages(waiting) and not st.outbox_items(),
          "an Outbox message can be deleted")
    graph.GraphClient.send_mail = real_send


class FakeToDo:
    """Microsoft To Do: two lists, a few tasks; records what's sent."""

    def __init__(self):
        day = lambda n: {"dateTime": f"{dt.date.today() + dt.timedelta(days=n)}T00:00:00",  # noqa: E731
                         "timeZone": "UTC"}
        self.lists = [{"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"},
                      {"id": "L2", "displayName": "Flagged email",
                       "wellknownListName": "flaggedEmails"}]
        self.tasks = {
            "t1": {"id": "t1", "title": "Pay the invoice", "status": "notStarted",
                   "importance": "high", "dueDateTime": day(-2), "_list": "L1"},
            "t2": {"id": "t2", "title": "Book the venue", "status": "notStarted",
                   "dueDateTime": day(3), "_list": "L1"},
            "t3": {"id": "t3", "title": "Q4 budget review", "status": "notStarted",
                   "_list": "L2"},
        }
        self.sent, self.n = [], 0

    def install(self):
        G = graph.GraphClient
        G.todo_lists = lambda c: [dict(x) for x in self.lists]
        G.tasks_delta = lambda c, lid, link=None: (
            [dict(t) for t in self.tasks.values() if t["_list"] == lid], f"d-{lid}")
        G.save_task = lambda c, lid, fields, tid=None: self._save(lid, fields, tid)
        G.delete_task = lambda c, lid, tid: (self.sent.append(("delete", tid)),
                                             self.tasks.pop(tid, None))
        G.task_links = lambda c, lid, tid: [{"displayName": "Q4 budget review",
                                             "webUrl": "https://outlook/m1",
                                             "externalId": "m1", "applicationName": "Outlook"}]
        G.message_link = lambda c, mid: f"https://outlook/{mid}"

    def _save(self, lid, fields, tid):
        self.sent.append((tid or "new", fields))
        if tid is None:
            self.n += 1
            tid = f"n{self.n}"
            self.tasks[tid] = {"id": tid, "_list": lid}
        t = self.tasks[tid]
        t.update({k: v for k, v in fields.items() if k != "linkedResources"})
        if fields.get("status") == "completed" and t.get("recurrence"):  # To Do: the next one
            due = dt.date.fromisoformat(t["dueDateTime"]["dateTime"][:10]) + dt.timedelta(7)
            self.n += 1
            nxt = dict(t, id=f"n{self.n}", status="notStarted",
                       dueDateTime={"dateTime": f"{due}T00:00:00", "timeZone": "UTC"})
            self.tasks[nxt["id"]] = nxt
        return dict(t)


def tasks_checks():
    from waddlemail.ui.task_editor import TaskEditor
    todo = FakeToDo()
    todo.install()
    w.refresh_tasks()
    wait(800)
    w.show_page("tasks")
    wait(200)
    page = w.tasks_page
    heads = [page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)
             for i in range(page.tree.topLevelItemCount())]
    check(heads == ["Overdue", "This week", "No date"] or heads == ["Overdue", "Next week",
                                                                    "No date"],
          "Tasks page: grouped by when they're due")
    late = page.tree.topLevelItem(0).child(0)
    check(late.text(2) == "Pay the invoice" and late.foreground(2).color().name() == "#c42b1c"
          and late.text(1) == "!", "an overdue task is red (and high priority marked)")
    check(w._nav["tasks"].text() == "Tasks (1)", "the rail shows how many are overdue")

    page.quick_add.setText("Call the plumber")
    page._quick_add()
    wait(500)
    made = todo.sent[-1][1]
    check(made["title"] == "Call the plumber"
          and made["dueDateTime"]["dateTime"].startswith(dt.date.today().isoformat()),
          "quick add: a task due today")

    page.reload_tasks()
    item = next(i for i in page._task_items() if i.text(2) == "Pay the invoice")
    item.setCheckState(0, Qt.CheckState.Checked)
    wait(500)
    check(("t1", {"status": "completed"}) in todo.sent and st.task("t1")["status"] == "completed"
          and "Pay the invoice" not in [i.text(2) for i in page._task_items()],
          "ticking a task completes it (and it leaves the list)")
    check(w._nav["tasks"].text() == "Tasks", "...nothing overdue now")

    w.set_task_due(["t2"], "tomorrow")
    wait(500)
    sent = todo.sent[-1]
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    check(sent[0] == "t2" and sent[1]["dueDateTime"]["dateTime"].startswith(tomorrow)
          and st.task("t2")["due"] == tomorrow, "Follow up > Tomorrow sets the due date")

    # the editor: a new task with a reminder
    dlg = w._task_editor(list_id="L1", title="Renew passport")
    dlg.set_due(dt.date.today() + dt.timedelta(days=7))
    dlg.has_reminder.setChecked(True)
    check(dlg.remind_day.date().toPython() == dt.date.today() + dt.timedelta(days=7),
          "editor: a reminder starts at 8:00 on the due date")
    dlg.importance.setCurrentIndex(2)
    w._save_task(dlg, None)
    wait(500)
    f = todo.sent[-1][1]
    check(f["title"] == "Renew passport" and f["isReminderOn"] and f["importance"] == "high",
          "editor: new task saved with its reminder and priority")

    # from an email
    opened = []
    real_exec = TaskEditor.exec
    TaskEditor.exec = lambda self: (opened.append(self), 0)[1]
    w.task_from_message("m1")
    wait(500)
    TaskEditor.exec = real_exec
    dlg = opened[-1] if opened else None
    check(dlg is not None and dlg.title.text() == "Q4 budget review"
          and "Final numbers attached" in dlg.notes.toPlainText()
          and dlg.links and dlg.links[0][2] == "m1" and dlg.has_due.isChecked(),
          "Create task from message: subject, text, due today, linked to the email")
    w._save_task(dlg, None)
    wait(500)
    check(todo.sent[-1][1]["linkedResources"][0]["externalId"] == "m1",
          "...and the task keeps the link")

    # a reminder goes off; dismissing turns it off
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    st.update_task("t2", reminder=past)
    w.settings.set("reminders/enabled", True)
    w.reminders.check()
    rw = w.reminder_window
    keys = rw.all_keys()
    check(("task:t2", past) in keys and rw.isVisible(), "a task's reminder shows with the rest")
    rw.dismissRequested.emit([k for k in keys if k[0] == "task:t2"])
    wait(500)
    check(st.task("t2")["reminder"] is None and ("t2", {"isReminderOn": False}) in todo.sent,
          "...and Dismiss turns it off in To Do")
    rw.dismissRequested.emit(rw.all_keys())
    rw.hide()

    # the flagged-email task opens its email
    page.reload()
    windows = len(w._windows)
    w.open_task_email("t3")
    wait(500)
    check(len(w._windows) == windows + 1, "a flagged-email task opens its email")
    for x in list(w._windows):
        x.close()
    w.delete_tasks(["t2"])
    wait(400)
    check(st.task("t2") is None and ("delete", "t2") in todo.sent, "a task can be deleted")

    # repeating: weekly from the due date; completing it brings the next one
    dlg = w._task_editor(list_id="L1", title="Timesheet")
    labels = [dlg.repeat.combo.itemText(i) for i in range(dlg.repeat.combo.count())]
    weekday = dt.date.today().strftime("%A")
    dlg.repeat.combo.setCurrentIndex(labels.index(f"Weekly on {weekday}"))
    dlg.repeat._chosen(dlg.repeat.combo.currentIndex())
    check(dlg.has_due.isChecked(), "choosing a repeat makes the task due (it repeats from it)")
    w._save_task(dlg, None)
    wait(500)
    rec = todo.sent[-1][1].get("recurrence") or {}
    check(rec.get("pattern", {}).get("daysOfWeek") == [weekday.lower()]
          and rec["range"]["startDate"] == dt.date.today().isoformat(),
          "a weekly task is saved with its pattern, from its due date")
    tid = next(k for k, t in todo.tasks.items() if t.get("title") == "Timesheet")
    page.reload_tasks()
    item = next(i for i in page._task_items() if i.data(0, Qt.ItemDataRole.UserRole) == tid)
    check(item.toolTip(3).startswith("Every week") or item.toolTip(3).startswith("Weekly")
          or "week" in item.toolTip(3).lower(), "...and shows that it repeats")
    w.complete_tasks([tid], True)
    wait(1200)
    nxt = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    check(any(r["title"] == "Timesheet" and r["due"] == nxt and r["status"] == "notStarted"
              for r in st.tasks()), "completing it brings the next one, a week on")
    dlg = w._task_editor(st.task(tid))
    check(dlg.repeat.combo.currentText() == f"Weekly on {weekday}",
          "editing shows its repeat")
    w.show_page("mail")


def travel_id():
    return folder_named("Travel")["id"]


QTimer.singleShot(1200, run)
sys.exit(app.exec())
