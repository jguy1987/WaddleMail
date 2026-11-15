"""Main window: folder pane | message list | reader. All network work runs off the UI thread."""
from __future__ import annotations

import datetime as dt
import logging

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QSplitter,
    QTextEdit, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
)

from .. import __version__
from ..mailboxes import MailboxManager
from ..settings import Settings
from ..store import Store
from . import iconset
from .dialogs import ComposeDialog, EventDialog, SharedMailboxDialog
from .folder_pane import FolderPane
from .preferences import PreferencesDialog
from .render import placeholder, render_body

log = logging.getLogger(__name__)

ID_ROLE = Qt.ItemDataRole.UserRole


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as exc:  # surfaced in the status bar
            log.exception("worker failed")
            self.signals.failed.emit(str(exc))
            return
        self.signals.done.emit(result)


class ReaderPage(QWebEnginePage):
    """Opens clicked links in the system browser instead of navigating the reader."""

    def javaScriptConsoleMessage(self, level, message, line, source):
        # Blocked remote images are expected (see the CSP); don't spam the terminal with them.
        if "Content Security Policy" in message:
            return
        super().javaScriptConsoleMessage(level, message, line, source)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            if url.scheme() in ("http", "https", "mailto"):
                QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


def _fmt_received(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    now = dt.datetime.now().astimezone()
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%Y-%m-%d")


class MainWindow(QMainWindow):
    def __init__(self, store: Store, mailboxes: MailboxManager, settings: Settings):
        super().__init__()
        self.store, self.mailboxes = store, mailboxes
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self._workers: set[Worker] = set()
        self._current_folder: str | None = None
        self._current_message: str | None = None
        self._current_body: tuple[str, str] | None = None
        self._syncing = False

        self.setWindowTitle("WaddleMail")
        self.resize(1200, 760)

        # folders
        self.folder_tree = FolderPane(store)
        self.folder_tree.setMinimumWidth(160)
        self.folder_tree.folderSelected.connect(self._on_folder_selected)
        self.folder_tree.removeMailboxRequested.connect(self.remove_mailbox)

        # messages
        self.msg_list = QTreeWidget()
        self.msg_list.setRootIsDecorated(False)
        self.msg_list.setUniformRowHeights(True)
        self.msg_list.setHeaderLabels(["From", "Subject", "Received"])
        self.msg_list.setColumnWidth(0, 180)
        self.msg_list.setColumnWidth(1, 320)
        self.msg_list.itemSelectionChanged.connect(self._on_message_selected)

        # reader: no JavaScript, no plugins, CSP blocks remote content by default
        self.reader = QWebEngineView()
        self.reader.setPage(ReaderPage(self.reader))
        s = self.reader.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.reader.setHtml(placeholder("Select a message."))

        split = QSplitter()
        mid = QSplitter(Qt.Orientation.Vertical)
        mid.addWidget(self.msg_list)
        mid.addWidget(self.reader)
        mid.setSizes([300, 460])
        split.addWidget(self.folder_tree)
        split.addWidget(mid)
        split.setStretchFactor(1, 1)  # resizing the window grows the mail, not the folders
        split.setSizes([240, 960])
        self.setCentralWidget(split)

        # Session toggle in the View menu; its starting value comes from Preferences.
        self.images_action = QAction("Load remote &images", self)
        self.images_action.setCheckable(True)
        self.images_action.setChecked(self.settings.get("images/load_remote"))
        self.images_action.toggled.connect(self._rerender_body)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.folder_tree.set_context_actions([self.act_add_shared, self.act_add_account])
        QApplication.instance().focusChanged.connect(self._update_edit_actions)
        self._update_edit_actions()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync_current)
        self._apply_settings()

        self._populate_folders()
        self._start_initial_sync()

    # -- background plumbing ------------------------------------------
    def _run(self, fn, on_done, *args, on_failed=None):
        w = Worker(fn, *args)
        self._workers.add(w)

        def cleanup():
            self._workers.discard(w)

        w.signals.done.connect(on_done)
        w.signals.done.connect(cleanup)
        w.signals.failed.connect(self._on_error)
        if on_failed is not None:
            w.signals.failed.connect(on_failed)
        w.signals.failed.connect(cleanup)
        self.pool.start(w)

    def _on_error(self, msg: str):
        self._syncing = False
        self.statusBar().showMessage(f"Error: {msg}", 10_000)

    # -- startup ------------------------------------------------------
    def _start_initial_sync(self):
        self.statusBar().showMessage("Signing in…")
        mailboxes = self.mailboxes

        def job():
            mailboxes.ensure_accounts()  # opens the browser on first run
            self._sync_all_folders()
            first = mailboxes.all()[0]
            inbox = self.store.well_known_folder(first.id, "inbox")
            error = None
            if inbox:
                try:
                    first.sync.sync_folder(inbox)
                except Exception as exc:  # offline: still show what's stored locally
                    log.warning("inbox sync failed: %s", exc)
                    error = str(exc)
            return inbox, error

        self._syncing = True
        self._run(job, self._initial_sync_done)

    def _sync_all_folders(self):
        """Refresh every mailbox's folder list and counts (worker thread). One mailbox failing,
        say a shared one whose access was taken away, doesn't stop the others."""
        for box in self.mailboxes.all():
            try:
                box.sync.sync_folders()
            except Exception as exc:
                log.warning("folder sync failed for %s: %s", box.display_name, exc)

    def _initial_sync_done(self, payload):
        inbox_id, error = payload
        self._populate_folders()
        if inbox_id:
            self.folder_tree.select_folder(inbox_id)  # the sync this starts is skipped below
        self._syncing = False
        if error:
            self.statusBar().showMessage(f"Couldn't sync, showing stored mail: {error}", 10_000)
        else:
            self.statusBar().showMessage("Up to date", 3000)
        self.timer.start()

    # -- folders ------------------------------------------------------
    def _populate_folders(self):
        self.folder_tree.populate()
        self._update_title()

    def _on_folder_selected(self, fid: str):
        if fid == self._current_folder:
            return
        self._current_folder = fid
        self._current_message = None
        self._update_title()
        self.reader.setHtml(placeholder("Select a message."))
        self._refresh_messages()  # instant, from the local store
        self.sync_current()       # then catch up in the background

    def _update_title(self):
        item = self.folder_tree.currentItem()
        box = self.mailboxes.for_folder(self._current_folder) if self._current_folder else None
        if item is None or box is None:
            self.setWindowTitle("WaddleMail")
        else:
            self.setWindowTitle(f"{item.text(0)} – {box.display_name} – WaddleMail")

    # -- mailboxes ----------------------------------------------------
    def add_account(self):
        if self._syncing:
            self.statusBar().showMessage("Wait for the current sync to finish", 4000)
            return
        self.statusBar().showMessage("Signing in… (finish in your browser)")
        self._syncing = True

        def job():
            box = self.mailboxes.add_account()
            box.sync.sync_folders()
            return box.id

        self._run(job, self._mailbox_added)

    def add_shared_mailbox(self):
        accounts = [(b.account_id, b.display_name) for b in self.mailboxes.all() if not b.shared]
        if not accounts:
            self.statusBar().showMessage("Sign in first", 4000)
            return
        dlg = SharedMailboxDialog(accounts, self)
        if not dlg.exec():
            return
        v = dlg.values()
        self.statusBar().showMessage(f"Opening {v['address']}…")
        self._syncing = True

        def job():
            return self.mailboxes.add_shared(v["account_id"], v["address"], v["display_name"]).id

        def failed(msg: str):
            QMessageBox.warning(
                self, "Add shared mailbox",
                f"Couldn't open {v['address']}.\n\nCheck the address and that you've been "
                f"given access to the mailbox or some of its folders.\n\n{msg}")

        self._run(job, self._mailbox_added, on_failed=failed)

    def _mailbox_added(self, mailbox_id: str):
        self._syncing = False
        self._populate_folders()
        inbox = self.store.well_known_folder(mailbox_id, "inbox")
        if inbox:
            self.folder_tree.select_folder(inbox)
        self.statusBar().showMessage("Mailbox added", 3000)

    def remove_mailbox(self, mailbox_id: str):
        box = self.mailboxes.get(mailbox_id)
        if box is None:
            return
        if box.shared:
            gone = {box.id}
            q = (f"Remove {box.display_name} from the folder pane?\n\nIts locally stored mail "
                 "is deleted; nothing changes on the server.")
        else:
            gone = {b.id for b in self.mailboxes.all() if b.account_id == box.account_id}
            q = (f"Sign out of {box.display_name}?\n\nIts locally stored mail and any shared "
                 "mailboxes opened with it are removed from this computer.")
        if QMessageBox.question(self, "WaddleMail", q) != QMessageBox.StandardButton.Yes:
            return
        if self._current_folder and self.store.folder_mailbox(self._current_folder) in gone:
            self._current_folder = self._current_message = None
            self.msg_list.clear()
            self.reader.setHtml(placeholder("Select a folder."))
        self.mailboxes.remove(mailbox_id)
        self._populate_folders()

    # -- messages -----------------------------------------------------
    def _refresh_messages(self):
        if not self._current_folder:
            return
        selected = self._current_message
        self.msg_list.blockSignals(True)
        self.msg_list.clear()
        bold = QFont()
        bold.setBold(True)
        restore = None
        for r in self.store.list_messages(self._current_folder, since=self._list_cutoff()):
            sender = r["sender_name"] or r["sender_addr"] or ""
            it = QTreeWidgetItem([sender, r["subject"] or "(no subject)", _fmt_received(r["received"])])
            it.setData(0, ID_ROLE, r["id"])
            it.setToolTip(1, r["preview"] or "")
            if not r["is_read"]:
                for col in range(3):
                    it.setFont(col, bold)
            self.msg_list.addTopLevelItem(it)
            if r["id"] == selected:
                restore = it
        if restore:
            self.msg_list.setCurrentItem(restore)
        self.msg_list.blockSignals(False)

    def sync_current(self):
        fid = self._current_folder
        box = self.mailboxes.for_folder(fid) if fid else None
        if box is None or self._syncing:
            return
        self._syncing = True
        self.statusBar().showMessage("Syncing…")

        max_age_days = self.settings.get("cache/max_age_days")  # read on the UI thread

        def job():
            res = box.sync.sync_folder(fid)
            self._sync_all_folders()  # refresh unread counts / new folders everywhere
            if max_age_days:
                self.store.purge_bodies_older_than(max_age_days)
            return fid, res

        self._run(job, self._sync_done)

    def _sync_done(self, payload):
        self._syncing = False
        fid, res = payload
        self._populate_folders()
        if fid == self._current_folder:
            self._refresh_messages()
        self.statusBar().showMessage(f"Synced (+{res.upserted} / -{res.removed})", 4000)

    # -- reader -------------------------------------------------------
    def _on_message_selected(self):
        items = self.msg_list.selectedItems()
        if not items:
            return
        mid = items[0].data(0, ID_ROLE)
        box = self.mailboxes.for_folder(self._current_folder)
        if box is None:
            return
        self._current_message = mid
        self._current_body = None
        self.reader.setHtml(placeholder("Loading…"))
        self._run(lambda: (mid, box.bodies.get(mid)), self._body_ready)

    def _body_ready(self, payload):
        mid, body = payload
        if mid != self._current_message:  # user already moved on
            return
        self._current_body = (body.content_type, body.content)
        self._rerender_body()
        self.statusBar().showMessage("Loaded from local cache" if body.from_cache
                                     else "Fetched from server and cached", 3000)

    def _rerender_body(self, *_):
        if self._current_body:
            ctype, content = self._current_body
            self.reader.setHtml(render_body(ctype, content, self.images_action.isChecked()))

    # -- menus ----------------------------------------------------------
    def _action(self, text: str, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(lambda checked=False: slot())
        return act

    def _themed(self, text: str, icon_name: str, slot, shortcut=None, enabled: bool = True,
                tip: str | None = None) -> QAction:
        """An action with a bundled Lucide icon (text only if the icon is missing)."""
        act = self._action(text, slot, shortcut)
        act.setIcon(iconset.load(icon_name))
        act.setToolTip(tip or text.replace("&", ""))
        act.setEnabled(enabled)
        return act

    def _build_actions(self):
        soon = "Coming soon"
        self.new_menu = QMenu("&New", self)
        self.new_menu.addAction(self._themed(
            "&Mail message", "mail-plus", self.new_mail, QKeySequence.StandardKey.New))
        self.new_menu.addAction(self._themed(
            "&Event", "calendar-plus", self.new_event, "Ctrl+Shift+E"))

        self.act_add_account = self._action("Add &account…", self.add_account)
        self.act_add_shared = self._action("Add &shared mailbox…", self.add_shared_mailbox)

        self.act_send_receive = self._themed(
            "Send/&Receive", "refresh-cw", self.sync_current, "F5",
            tip="Send and receive mail (F5)")

        self.act_cut = self._themed("Cu&t", "scissors", self._edit_cut, QKeySequence.StandardKey.Cut)
        self.act_copy = self._themed("&Copy", "copy", self._edit_copy, QKeySequence.StandardKey.Copy)
        self.act_paste = self._themed("&Paste", "clipboard-paste", self._edit_paste, QKeySequence.StandardKey.Paste)
        self.act_select_all = self._themed(
            "Select &all", "", self._edit_select_all, QKeySequence.StandardKey.SelectAll)

        # Placeholders until the outbox / message actions exist.
        self.act_reply = self._themed("Reply", "reply", lambda: None, enabled=False, tip=soon)
        self.act_reply_all = self._themed("Reply all", "reply-all", lambda: None, enabled=False, tip=soon)
        self.act_forward = self._themed("Forward", "forward", lambda: None, enabled=False, tip=soon)
        self.act_delete = self._themed("Delete", "trash-2", lambda: None, enabled=False, tip=soon)

    def _build_toolbar(self):
        bar = QToolBar("Main", self)
        bar.setObjectName("main-toolbar")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(bar)
        self.toolbar = bar

        new_btn = QToolButton()
        new_btn.setText("New")
        new_btn.setIcon(iconset.load("mail-plus"))
        new_btn.setToolTip("New mail message (Ctrl+N); use the arrow for more")
        new_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        new_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        new_btn.setMenu(self.new_menu)
        new_btn.clicked.connect(lambda checked=False: self.new_mail())
        bar.addWidget(new_btn)
        bar.addAction(self.act_send_receive)
        bar.addSeparator()
        for act in (self.act_cut, self.act_copy, self.act_paste):
            bar.addAction(act)
        bar.addSeparator()
        for act in (self.act_reply, self.act_reply_all, self.act_forward, self.act_delete):
            bar.addAction(act)

    # -- cut / copy / paste: act on whatever has focus -------------------
    @staticmethod
    def _focused_text_widget():
        w = QApplication.focusWidget()
        return w if isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit)) else None

    def _reader_has_focus(self) -> bool:
        w = QApplication.focusWidget()
        return w is not None and (w is self.reader or self.reader.isAncestorOf(w))

    def _edit_cut(self):
        t = self._focused_text_widget()
        if t is not None and not t.isReadOnly():
            t.cut()

    def _edit_copy(self):
        t = self._focused_text_widget()
        if t is not None:
            t.copy()
        elif self._reader_has_focus():
            self.reader.page().triggerAction(QWebEnginePage.WebAction.Copy)

    def _edit_paste(self):
        t = self._focused_text_widget()
        if t is not None and not t.isReadOnly():
            t.paste()

    def _edit_select_all(self):
        t = self._focused_text_widget()
        if t is not None:
            t.selectAll()
        elif self._reader_has_focus():
            self.reader.page().triggerAction(QWebEnginePage.WebAction.SelectAll)

    def _update_edit_actions(self, *_):
        t = self._focused_text_widget()
        editable = t is not None and not t.isReadOnly()
        readable = t is not None or self._reader_has_focus()
        self.act_cut.setEnabled(editable)
        self.act_paste.setEnabled(editable)
        self.act_copy.setEnabled(readable)
        self.act_select_all.setEnabled(readable)

    def _build_menus(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        file_menu.addMenu(self.new_menu)          # same menu the toolbar's New button drops down
        file_menu.addSeparator()
        file_menu.addAction(self.act_send_receive)
        file_menu.addSeparator()
        file_menu.addAction(self.act_add_account)
        file_menu.addAction(self.act_add_shared)
        file_menu.addSeparator()
        file_menu.addAction(self._action("&Quit", self.close, QKeySequence.StandardKey.Quit))

        edit_menu = mb.addMenu("&Edit")
        for act in (self.act_cut, self.act_copy, self.act_paste):
            edit_menu.addAction(act)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_select_all)
        edit_menu.addSeparator()
        edit_menu.addAction(self._action("&Preferences…", self.show_preferences, "Ctrl+,"))

        view_menu = mb.addMenu("&View")
        view_menu.addAction(self.images_action)  # same action as the toolbar button
        folders = QAction("Show &folder pane", self)
        folders.setCheckable(True)
        folders.setChecked(True)
        folders.toggled.connect(self.folder_tree.setVisible)
        view_menu.addAction(folders)

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self._action("&About WaddleMail", self.show_about))

    def show_about(self):
        QMessageBox.about(
            self, "About WaddleMail",
            f"WaddleMail {__version__}\n\nA Linux mail client for Microsoft 365.")

    def new_mail(self):
        dlg = ComposeDialog(self)
        if dlg.exec():
            _values = dlg.values()  # TODO: queue in outbox, then POST /me/sendMail
            self.statusBar().showMessage("Sending isn't wired up yet", 5000)

    def new_event(self):
        dlg = EventDialog(self)
        if dlg.exec():
            _values = dlg.values()  # TODO: POST /me/events
            self.statusBar().showMessage("Saving events isn't wired up yet", 5000)

    # -- preferences ----------------------------------------------------
    def show_preferences(self):
        dlg = PreferencesDialog(self.settings, self._clear_cache, self)
        if dlg.exec():
            self._apply_settings()
            self._refresh_messages()
            self.statusBar().showMessage("Preferences saved", 3000)

    def _apply_settings(self):
        s = self.settings
        self.mailboxes.configure(s.get("sync/window_days") or 36500,
                                 s.get("cache/max_mb") * 1024 * 1024)
        self.timer.setInterval(s.get("sync/interval_min") * 60_000)

    def _list_cutoff(self) -> str | None:
        days = self.settings.get("list/window_days")
        if not days:
            return None
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _clear_cache(self):
        self.store.clear_bodies()
        self.statusBar().showMessage("Cached message bodies cleared", 3000)
