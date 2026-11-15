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
    pane.select_folder(made["id"])
    QTest.keyClick(pane, Qt.Key.Key_Delete)
    wait(400)
    check(st.folder_row(made["id"]) is None and w._current_folder == "in",
          "deleting it from Deleted Items removes it; Inbox is shown")


QTimer.singleShot(1200, run)
sys.exit(app.exec())
