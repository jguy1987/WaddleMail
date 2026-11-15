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
from waddlemail.ui.message_list import COL, ID_ROLE  # noqa: E402


def offline(self, url, params=None, headers=None, retries=5, method="GET", json=None):
    raise graph.GraphError("offline", 503)


graph.GraphClient._request = offline


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


def m(i, subject, name, hours, read=False, att=False, flag="notFlagged"):
    return {"id": i, "subject": subject,
            "from": {"emailAddress": {"name": name, "address": f"{name.split()[0]}@x.com"}},
            "receivedDateTime": (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "isRead": read, "hasAttachments": att, "flag": {"flagStatus": flag}}


st.apply_message_changes("in", [
    m("m1", "Q4 budget review", "Priya Shah", 1, att=True),
    m("m2", "Re: Offsite agenda", "Marcus Webb", 3, read=True),
    m("m3", "Build failed", "CI Bot", 20),
    m("m4", "Contract draft", "Alex Kim", 80, read=True, att=True),
    m("m5", "Newsletter", "Contoso News", 200),
])
st.put_body("m1", "html", "<p>Final numbers attached.</p>", 10**8)

w = MainWindow(st, MailboxManager(st, FakeAuth()), Settings(str(tmp / "settings.ini")))
w.resize(1280, 760)
w.show()
view = w.msg_list
failures = 0


def check(cond, what):
    global failures
    print(("ok   " if cond else "FAIL ") + what)
    failures += not cond


def ids():
    return [view.proxy.index(r, 0).data(ID_ROLE) for r in range(view.proxy.rowCount())]


def unread():
    return {r["id"] for r in st.list_messages("in") if not r["is_read"]}


def point(mid, col):
    return view.visualRect(view.proxy.index(ids().index(mid), col)).center()


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
    check(ids() == ["m1", "m2", "m3", "m4", "m5"], "newest first")
    click("m1")
    QTest.qWait(200)
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
    check(set(ids()[:2]) == {"m1", "m4"}, "sort by attachment")
    view.sortByColumn(COL["received"], Qt.SortOrder.DescendingOrder)

    w._focus_filter()
    QTest.keyClicks(w.filter_bar.search, "contract alex")
    QTest.qWait(400)
    check(ids() == ["m4"], "quick filter: every word matches somewhere")
    QTest.keyClick(w.filter_bar.search, Qt.Key.Key_Escape)
    w.filter_bar.unread.setChecked(True)
    check(set(ids()) == unread(), "unread toggle")
    w.filter_bar.unread.setChecked(False)

    w.set_reading_pane("right")
    QTest.qWait(200)
    check(view.compact and view.header().isHidden(), "right pane: compact rows")
    r = view.visualRect(view.proxy.index(ids().index("m5"), COL["subject"]))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(r.left() + 10, r.center().y()))
    check("m5" not in unread(), "compact: dot click toggles read")

    w.set_reading_pane("off")
    QTest.qWait(300)
    check(not view.compact and w.msg_view.isHidden(), "reading pane off")
    click("m3")
    w.mark_read(["m3"], False)
    click("m2")
    click("m3")
    QTest.qWait(200)
    check("m3" in unread(), "pane off: selecting doesn't mark read")
    double_click("m3")
    QTest.qWait(300)
    check(len(w._windows) == 1 and "m3" not in unread(), "double-click opens and marks read")

    w.close()
    saved = Settings(str(tmp / "settings.ini"))
    check(saved.get("view/reading_pane") == "off" and not saved.get("list/header").isEmpty(),
          "layout saved on exit")
    app.exit(1 if failures else 0)


QTimer.singleShot(1200, run)
sys.exit(app.exec())
