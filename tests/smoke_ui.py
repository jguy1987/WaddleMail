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

    def notify(self, summary, body, key, origin=""):
        self.sent.append((summary, body, key, origin))

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


def offline(self, url, params=None, headers=None, retries=5, method="GET", json=None):
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
    QTest.keyClick(w, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    wait(100)
    check(w.pages.currentWidget() is view and w._nav["contacts"].isChecked(),
          "Ctrl+2 / the rail switch to Contacts")
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


def travel_id():
    return folder_named("Travel")["id"]


QTimer.singleShot(1200, run)
sys.exit(app.exec())
