"""Main window: folder pane | message list | reading pane. Network work runs off the UI thread."""
from __future__ import annotations

import datetime as dt
import logging

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QSplitter,
    QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .. import __version__
from ..mailboxes import MailboxManager
from ..settings import Settings
from ..store import Store
from . import iconset
from .dialogs import ComposeDialog, EventDialog, SharedMailboxDialog
from .folder_pane import FolderPane
from .message_list import OUTGOING, FilterBar, MessageListView
from .message_view import MessageView, MessageWindow
from .preferences import PreferencesDialog
from .render import placeholder

log = logging.getLogger(__name__)

READING_PANE_MODES = (("right", "&Right"), ("bottom", "&Bottom"), ("off", "&Off"))


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


class MainWindow(QMainWindow):
    def __init__(self, store: Store, mailboxes: MailboxManager, settings: Settings):
        super().__init__()
        self.store, self.mailboxes = store, mailboxes
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self._workers: set[Worker] = set()
        self._windows: set[MessageWindow] = set()
        self._current_folder: str | None = None
        self._current_message: str | None = None  # the one selected message, if exactly one
        self._syncing = False  # one sync/send job at a time

        self.setWindowTitle("WaddleMail")
        self.resize(1200, 760)

        # folders
        self.folder_tree = FolderPane(store)
        self.folder_tree.setMinimumWidth(160)
        self.folder_tree.folderSelected.connect(self._on_folder_selected)
        self.folder_tree.removeMailboxRequested.connect(self.remove_mailbox)

        # messages: quick filter bar over the list
        self.filter_bar = FilterBar()
        self.filter_bar.changed.connect(self._refresh_messages)
        self.msg_list = MessageListView()
        self.msg_list.selectionModel().selectionChanged.connect(self._on_message_selected)
        self.msg_list.openRequested.connect(self.open_message)
        self.msg_list.toggleReadRequested.connect(self._toggle_read)
        self.msg_list.toggleFlagRequested.connect(self._toggle_flag)
        self.filter_bar.sort_button.clicked.connect(self._show_sort_menu)
        self.msg_list.header().sortIndicatorChanged.connect(self._update_sort_button)
        list_box = QWidget()
        lay = QVBoxLayout(list_box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.filter_bar)
        lay.addWidget(self.msg_list)

        self.msg_view = MessageView()

        self.content = QSplitter()
        self.content.addWidget(list_box)
        self.content.addWidget(self.msg_view)
        self.content.setChildrenCollapsible(False)
        self.outer = QSplitter()
        self.outer.addWidget(self.folder_tree)
        self.outer.addWidget(self.content)
        self.outer.setStretchFactor(1, 1)  # resizing the window grows the mail, not the folders
        self.outer.setSizes([240, 960])
        self.setCentralWidget(self.outer)

        # Session toggle in the View menu; its starting value comes from Preferences.
        self.images_action = QAction("Load remote &images", self)
        self.images_action.setCheckable(True)
        self.images_action.setChecked(self.settings.get("images/load_remote"))
        self.images_action.toggled.connect(self.msg_view.rerender)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.folder_tree.set_context_actions([self.act_add_shared, self.act_add_account])
        QApplication.instance().focusChanged.connect(self._update_edit_actions)
        self._update_edit_actions()
        self._update_message_actions()

        # Marking read after a delay in the reading pane; sending local changes shortly after.
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.timeout.connect(self._mark_viewed_read)
        self._send_timer = QTimer(self)
        self._send_timer.setSingleShot(True)
        self._send_timer.setInterval(1500)
        self._send_timer.timeout.connect(self._send_changes)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync_current)
        self._apply_settings()
        self._restore_layout()

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
        self.statusBar().showMessage(f"Error: {msg}", 10_000)

    def _sync_failed(self, _msg: str):
        self._syncing = False

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
        self._run(job, self._initial_sync_done, on_failed=self._sync_failed)

    def _sync_all_folders(self):
        """Send queued changes and refresh every mailbox's folder list and counts (worker
        thread). One mailbox failing, say a shared one whose access was taken away, doesn't
        stop the others."""
        for box in self.mailboxes.all():
            try:
                box.sync.send_changes()
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
        self.msg_view.show_placeholder("Select a message.")
        self.filter_bar.clear(emit=False)  # a filter belongs to the folder it was typed in
        self.msg_list.clearSelection()
        self._refresh_messages()  # instant, from the local store
        self.msg_list.scrollToTop()
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

        self._run(job, self._mailbox_added, on_failed=self._sync_failed)

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
            self._syncing = False
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
            self.msg_list.set_messages([], False)
            self.msg_view.show_placeholder("Select a folder.")
        self.mailboxes.remove(mailbox_id)
        self._populate_folders()

    # -- message list ---------------------------------------------------
    def _refresh_messages(self):
        if not self._current_folder:
            return
        rows = self.store.list_messages(self._current_folder, since=self._list_cutoff(),
                                        **self.filter_bar.criteria())
        folder = self.store.folder_row(self._current_folder)
        self.msg_list.set_messages(rows, bool(folder) and folder["well_known"] in OUTGOING)
        self.filter_bar.set_count(len(rows))
        self._update_message_actions()

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

        self._run(job, self._sync_done, on_failed=self._sync_failed)

    def _sync_done(self, payload):
        self._syncing = False
        fid, res = payload
        self._populate_folders()
        if fid == self._current_folder:
            self._refresh_messages()
        self.statusBar().showMessage(f"Synced (+{res.upserted} / -{res.removed})", 4000)

    def _on_message_selected(self, *_):
        self._update_message_actions()
        self._read_timer.stop()
        ids = self.msg_list.selected_ids()
        if len(ids) != 1:
            self._current_message = None
            self.msg_view.show_placeholder(
                f"{len(ids)} messages selected." if ids else "Select a message.")
            return
        if ids[0] == self._current_message:
            return
        self._current_message = ids[0]
        if not self.msg_view.isHidden():  # reading pane is on
            self._show_in_pane(ids[0])

    def _show_in_pane(self, mid: str):
        row = self.store.message_row(mid)
        box = self.mailboxes.for_folder(self._current_folder)
        if row is None or box is None:
            return
        self.msg_view.show_message(row)
        self._run(lambda: (mid, box.bodies.get(mid)), self._body_ready,
                  on_failed=lambda msg: self._body_failed(mid, msg))
        if self.settings.get("read/mark_on_view") and not row["is_read"]:
            self._read_timer.start(self.settings.get("read/mark_delay_sec") * 1000)

    def _body_ready(self, payload):
        mid, body = payload
        if mid != self.msg_view.message_id:  # user already moved on
            return
        self.msg_view.set_body(body.content_type, body.content, self.images_action.isChecked())
        self.statusBar().showMessage("Loaded from local cache" if body.from_cache
                                     else "Fetched from server and cached", 3000)

    def _body_failed(self, mid: str, msg: str):
        if mid == self.msg_view.message_id:
            self.msg_view.reader.setHtml(placeholder(f"Couldn't load this message: {msg}"))

    def open_message(self, mid: str):
        """Open one message in its own window (double-click / Enter) and mark it read."""
        row = self.store.message_row(mid)
        box = self.mailboxes.for_folder(self._current_folder) if self._current_folder else None
        if row is None or box is None:
            return
        win = MessageWindow(row, self.images_action.isChecked(),
                            [self.act_reply, self.act_reply_all, self.act_forward,
                             self.act_delete], self)
        self._windows.add(win)
        win.destroyed.connect(lambda *_: self._windows.discard(win))
        win.show()
        self._run(lambda: box.bodies.get(mid),
                  lambda body: win.set_body(body.content_type, body.content),
                  on_failed=win.show_error)
        self.mark_read([mid], True)

    # -- read / flag ------------------------------------------------------
    def _mark_viewed_read(self):
        if self._current_message and not self.msg_view.isHidden() \
                and self.msg_view.message_id == self._current_message:
            self.mark_read([self._current_message], True)

    def mark_read(self, ids: list[str], read: bool):
        box = self.mailboxes.for_folder(self._current_folder) if self._current_folder else None
        if box is None or not ids:
            return
        self._local_change(self.store.set_read(box.id, ids, read))

    def set_flag(self, ids: list[str], status: str):
        box = self.mailboxes.for_folder(self._current_folder) if self._current_folder else None
        if box is None or not ids:
            return
        self._local_change(self.store.set_flag(box.id, ids, status))

    def _toggle_read(self, ids: list[str]):
        rows = [self.store.message_row(i) for i in ids]
        self.mark_read(ids, any(r and not r["is_read"] for r in rows))

    def _toggle_flag(self, ids: list[str]):
        rows = [self.store.message_row(i) for i in ids]
        flag_them = any(r and r["flag"] != "flagged" for r in rows)
        self.set_flag(ids, "flagged" if flag_them else "notFlagged")

    def _local_change(self, changed: list[str]):
        """Show a local read/flag change at once; it goes to the server shortly after."""
        if not changed:
            return
        self.msg_list.model_.update_rows([self.store.message_row(i) for i in changed])
        self.folder_tree.refresh_counts()
        self._send_timer.start()

    def _send_changes(self):
        if self._syncing:  # try again once the running job is done
            self._send_timer.start()
            return
        self._syncing = True
        boxes = self.mailboxes.all()

        def job():
            for box in boxes:
                try:
                    box.sync.send_changes()
                except Exception as exc:  # offline: stays queued for the next sync
                    log.warning("sending changes for %s failed: %s", box.display_name, exc)

        self._run(job, self._changes_sent, on_failed=self._sync_failed)

    def _changes_sent(self, _):
        self._syncing = False
        # A change the server refused was undone in the store; show that.
        self._refresh_messages()
        self.folder_tree.refresh_counts()

    def _update_message_actions(self, *_):
        any_selected = bool(self.msg_list.selected_ids())
        for act in self._message_actions:
            act.setEnabled(any_selected)

    # -- reading pane / layout -----------------------------------------
    def set_reading_pane(self, mode: str):
        if mode not in dict(READING_PANE_MODES):
            mode = "bottom"
        prev = getattr(self, "_pane_mode", None)
        if prev in ("right", "bottom"):
            self._split_states[prev] = self.content.saveState()
        self._pane_mode = mode
        self._pane_actions[mode].setChecked(True)
        # Beside the reading pane the list is narrow: two-line rows instead of columns.
        self.msg_list.set_compact(mode == "right")
        self.filter_bar.set_compact(mode == "right")
        self._update_sort_button()
        if mode == "off":
            self.msg_view.hide()
            self._read_timer.stop()
            return
        self.content.setOrientation(Qt.Orientation.Horizontal if mode == "right"
                                    else Qt.Orientation.Vertical)
        state = self._split_states.get(mode)
        if not (state and not state.isEmpty() and self.content.restoreState(state)):
            total = sum(self.content.sizes()) or 1000
            share = 0.45 if mode == "right" else 0.4  # the list's share
            self.content.setSizes([int(total * share), int(total * (1 - share))])
        self.msg_view.show()
        if self._current_message and self.msg_view.message_id != self._current_message:
            self._show_in_pane(self._current_message)

    def _restore_layout(self):
        s = self.settings
        geometry = s.get("window/geometry")
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        split = s.get("window/folder_split")
        if not split.isEmpty():
            self.outer.restoreState(split)
        self._split_states = {"right": s.get("window/split_right"),
                              "bottom": s.get("window/split_bottom")}
        self.msg_list.restore_header(s.get("list/header"))
        self.act_filter_bar.setChecked(s.get("view/filter_bar"))
        self.filter_bar.setVisible(s.get("view/filter_bar"))
        self.set_reading_pane(s.get("view/reading_pane"))

    def closeEvent(self, event):
        s = self.settings
        if self._pane_mode in ("right", "bottom"):
            self._split_states[self._pane_mode] = self.content.saveState()
        s.set("window/geometry", self.saveGeometry())
        s.set("window/folder_split", self.outer.saveState())
        s.set("window/split_right", self._split_states["right"])
        s.set("window/split_bottom", self._split_states["bottom"])
        s.set("list/header", self.msg_list.header_state())
        s.set("view/reading_pane", self._pane_mode)
        s.set("view/filter_bar", self.act_filter_bar.isChecked())
        for win in list(self._windows):
            win.close()
        super().closeEvent(event)

    def _show_sort_menu(self):
        button = self.filter_bar.sort_button
        self.msg_list.sort_menu().exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _update_sort_button(self, *_):
        self.filter_bar.sort_button.setText(self.msg_list.sort_label())

    def _set_filter_bar(self, on: bool):
        self.filter_bar.setVisible(on)
        if not on:
            self.filter_bar.clear(emit=False)
        self._refresh_messages()

    def _focus_filter(self):
        self.act_filter_bar.setChecked(True)
        self.filter_bar.focus_search()

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

    def _list_action(self, text: str, slot, shortcut=None, icon: str = "") -> QAction:
        """A message action: in the Message menu and the list's context menu. Its shortcut
        works while the list has focus, so single keys (M, S) don't fire while typing."""
        act = self._themed(text, icon, slot, shortcut)
        act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.msg_list.addAction(act)
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

        # Message actions; they work on the selection. "\tEnter" only labels the key: the
        # list handles Enter itself.
        sel = self.msg_list.selected_ids
        self.act_open = self._list_action(
            "&Open in new window\tEnter",
            lambda: self.msg_list.current_id() and self.open_message(self.msg_list.current_id()))
        self.msg_list.addAction(self._separator())
        self.act_mark_read = self._list_action(
            "Mark as &read", lambda: self.mark_read(sel(), True), icon="mail")
        self.act_mark_unread = self._list_action(
            "Mark as &unread", lambda: self.mark_read(sel(), False), "Ctrl+U")
        self.act_toggle_read = self._list_action(
            "&Toggle read/unread", lambda: self._toggle_read(sel()), "M")
        self.msg_list.addAction(self._separator())
        self.act_toggle_flag = self._list_action(
            "Toggle &flag", lambda: self._toggle_flag(sel()), "S", icon="flag")
        self.act_flag_complete = self._list_action(
            "Mark flag &complete", lambda: self.set_flag(sel(), "complete"), icon="check")
        self._message_actions = [self.act_open, self.act_mark_read, self.act_mark_unread,
                                 self.act_toggle_read, self.act_toggle_flag,
                                 self.act_flag_complete]

        self.act_filter_bar = QAction("&Quick filter bar", self)
        self.act_filter_bar.setCheckable(True)
        self.act_filter_bar.toggled.connect(self._set_filter_bar)
        self.act_focus_filter = self._action("&Filter messages…", self._focus_filter,
                                             "Ctrl+Shift+K")
        self._pane_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        for mode, label in READING_PANE_MODES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.triggered.connect(lambda checked=False, m=mode: self.set_reading_pane(m))
            group.addAction(act)
            self._pane_actions[mode] = act

    def _separator(self) -> QAction:
        act = QAction(self)
        act.setSeparator(True)
        return act

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
        reader = self.msg_view.reader
        return w is not None and (w is reader or reader.isAncestorOf(w))

    def _edit_cut(self):
        t = self._focused_text_widget()
        if t is not None and not t.isReadOnly():
            t.cut()

    def _edit_copy(self):
        t = self._focused_text_widget()
        if t is not None:
            t.copy()
        elif self._reader_has_focus():
            self.msg_view.reader.page().triggerAction(QWebEnginePage.WebAction.Copy)

    def _edit_paste(self):
        t = self._focused_text_widget()
        if t is not None and not t.isReadOnly():
            t.paste()

    def _edit_select_all(self):
        t = self._focused_text_widget()
        if t is not None:
            t.selectAll()
        elif self._reader_has_focus():
            self.msg_view.reader.page().triggerAction(QWebEnginePage.WebAction.SelectAll)
        elif QApplication.focusWidget() is self.msg_list:
            self.msg_list.selectAll()

    def _update_edit_actions(self, *_):
        t = self._focused_text_widget()
        editable = t is not None and not t.isReadOnly()
        readable = t is not None or self._reader_has_focus()
        self.act_cut.setEnabled(editable)
        self.act_paste.setEnabled(editable)
        self.act_copy.setEnabled(readable)
        self.act_select_all.setEnabled(readable or QApplication.focusWidget() is self.msg_list)

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
        edit_menu.addAction(self.act_focus_filter)
        edit_menu.addSeparator()
        edit_menu.addAction(self._action("&Preferences…", self.show_preferences, "Ctrl+,"))

        view_menu = mb.addMenu("&View")
        pane_menu = view_menu.addMenu("&Reading pane")
        for mode, _label in READING_PANE_MODES:
            pane_menu.addAction(self._pane_actions[mode])
        view_menu.addAction(self.act_filter_bar)
        folders = QAction("Show &folder pane", self)
        folders.setCheckable(True)
        folders.setChecked(True)
        folders.toggled.connect(self.folder_tree.setVisible)
        view_menu.addAction(folders)
        view_menu.addSeparator()
        view_menu.addAction(self.images_action)

        msg_menu = mb.addMenu("&Message")
        msg_menu.addAction(self.act_open)
        msg_menu.addSeparator()
        for act in (self.act_mark_read, self.act_mark_unread, self.act_toggle_read):
            msg_menu.addAction(act)
        msg_menu.addSeparator()
        msg_menu.addAction(self.act_toggle_flag)
        msg_menu.addAction(self.act_flag_complete)

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
