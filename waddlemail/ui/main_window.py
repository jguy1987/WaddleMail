"""Main window: folder pane | message list | reading pane. Network work runs off the UI thread."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import tempfile

from PySide6.QtCore import (
    QObject, QRunnable, QStandardPaths, Qt, QThreadPool, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QSplitter,
    QStackedWidget, QSystemTrayIcon, QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .. import __version__, desktop, graph
from ..attachments import Attachment, file_name, reconcile, reconcile_inline, unique_path
from ..calendars import event_row as calendar_event_row
from ..calendars import event_span
from ..contacts import personal_row, removal_payload, suggestions
from ..graph import NO_SUBFOLDERS
from ..inline import cid_refs, resolve_cids
from ..mailboxes import MailboxManager
from ..notify import make_notifier
from ..settings import Settings
from ..store import Store
from . import iconset
from .contacts_view import ContactsView
from .calendar_view import CalendarPage
from .compose_window import ComposeWindow
from .dialogs import SharedMailboxDialog
from .event_editor import EventEditor
from .reminders import ReminderService, ReminderWindow
from .folder_pane import FolderPane
from .folder_picker import TOP, FolderPicker
from .message_list import OUTGOING, FilterBar, MessageListView, OlderMailBar
from .message_view import MessageView, MessageWindow
from .preferences import PreferencesDialog
from .render import placeholder
from .status_indicator import StatusIndicator
from .tray import Tray

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
        # Server search for the quick filter: hits per (folder, words), and a generation
        # number so a slow answer to an older query is ignored.
        self._server_hits: dict[tuple[str, str], list[str]] = {}
        self._search_note = ""
        self._search_gen = 0
        self._pending_search: tuple[str, str] | None = None

        self.setWindowTitle("WaddleMail")
        self.resize(1200, 760)

        # Bottom right: connection state and what's happening.
        self.status = StatusIndicator(self.store.pending_count)
        self.status.clicked.connect(self.sync_current)
        self.statusBar().addPermanentWidget(self.status)
        graph.status_hook = self.status.report

        # folders
        self.folder_tree = FolderPane(store)
        self.folder_tree.setMinimumWidth(160)
        self.folder_tree.folderSelected.connect(self._on_folder_selected)
        self.folder_tree.removeMailboxRequested.connect(self.remove_mailbox)
        self.folder_tree.createFolderRequested.connect(self._create_folder)
        self.folder_tree.renameFolderRequested.connect(self._rename_folder)
        self.folder_tree.deleteFolderRequested.connect(self.delete_folder)
        self.folder_tree.moveFolderRequested.connect(self.move_folder)
        self.folder_tree.pickMoveFolderRequested.connect(self.pick_move_folder)
        self.folder_tree.messagesDropped.connect(
            lambda ids, dest, copy: self.move_messages(ids, dest, copy))
        self._last_dest: dict[str, str] = {}  # mailbox -> last Move/Copy destination

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
        self.older_bar = OlderMailBar()
        self.older_bar.requested.connect(self.download_older)
        self._downloading: set[str] = set()  # folders fetching older mail now
        self._no_older: set[str] = set()     # folders where the server had nothing older
        lay.addWidget(self.older_bar)

        self.msg_view = MessageView()
        self._connect_attachments(self.msg_view)
        self._save_dir: str | None = None  # where attachments were last saved

        self.content = QSplitter()
        self.content.addWidget(list_box)
        self.content.addWidget(self.msg_view)
        self.content.setChildrenCollapsible(False)
        self.outer = QSplitter()
        self.outer.addWidget(self.folder_tree)
        self.outer.addWidget(self.content)
        self.outer.setStretchFactor(1, 1)  # resizing the window grows the mail, not the folders
        self.outer.setSizes([240, 960])
        # Mail and Contacts are pages of one window, switched with the rail on the left.
        self.contacts_view = ContactsView(store, lambda: suggestions(self.store))
        self.contacts_view.composeRequested.connect(lambda to: self.new_mail(to))
        self.contacts_view.refreshRequested.connect(self.refresh_contacts)
        self.contacts_view.saveRequested.connect(self.save_contact)
        self.contacts_view.deleteRequested.connect(self.delete_contact)
        self.contacts_view.groupSaveRequested.connect(self.save_group)
        self.contacts_view.groupDeleteRequested.connect(self.delete_group)
        self.contacts_view.photoWanted.connect(self.fetch_photo)
        self._photos_pending: set[str] = set()
        self.calendar_page = CalendarPage(store)
        cp = self.calendar_page
        cp.rangeChanged.connect(lambda a, b: self._calendar_fetch_soon.start())
        cp.newEventRequested.connect(lambda s, e, all_day: self.new_event(s, e, all_day))
        cp.editRequested.connect(self.edit_event)
        cp.deleteRequested.connect(self.delete_event)
        cp.respondRequested.connect(self.respond_event)
        cp.refreshRequested.connect(lambda: self.refresh_calendar(calendars=True))
        cp.calendarToggled.connect(self.store.set_calendar_visible)
        self._calendar_fetch_soon = QTimer(self)  # wait for clicking through weeks to stop
        self._calendar_fetch_soon.setSingleShot(True)
        self._calendar_fetch_soon.setInterval(300)
        self._calendar_fetch_soon.timeout.connect(self.refresh_calendar)
        self._calendar_busy = False
        self.reminder_window = ReminderWindow(self)
        rw = self.reminder_window
        rw.dismissRequested.connect(self.dismiss_reminders)
        rw.snoozeRequested.connect(self.snooze_reminders)
        rw.openRequested.connect(self._open_event)
        rw.joinRequested.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        self.pages = QStackedWidget()
        self.pages.addWidget(self.outer)
        self.pages.addWidget(self.calendar_page)
        self.pages.addWidget(self.contacts_view)
        self.setCentralWidget(self.pages)
        self._contacts_busy = False

        # Session toggle in the View menu; its starting value comes from Preferences.
        self.images_action = QAction("Load remote &images", self)
        self.images_action.setCheckable(True)
        self.images_action.setChecked(self.settings.get("images/load_remote"))
        self.images_action.toggled.connect(self.msg_view.rerender)

        self._build_actions()
        self._build_nav()
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
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(600)  # after typing pauses
        self._search_timer.timeout.connect(self._search_server)

        # System integration: tray icon, desktop notifications, task manager badge.
        self._quitting = False
        self.tray = Tray(self, self.sync_current, self.quit_app)
        self.notifier = make_notifier(self.tray)
        self.notifier.activated.connect(self._open_from_notification)
        self.reminders = ReminderService(self.store, self.notifier, self.reminder_window,
                                         lambda: self.settings.get("reminders/enabled"))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync_current)
        self.calendar_timer = QTimer(self)  # meetings arrive and move: every few minutes
        self.calendar_timer.setInterval(5 * 60_000)
        self.calendar_timer.timeout.connect(self.refresh_calendar)
        self.contacts_timer = QTimer(self)  # contacts change slowly: hourly is plenty
        self.contacts_timer.setInterval(60 * 60_000)
        self.contacts_timer.timeout.connect(self.refresh_contacts)
        self._apply_settings()
        self._restore_layout()

        self._populate_folders()
        self._start_initial_sync()

    # -- background plumbing ------------------------------------------
    def _run(self, fn, on_done, *args, on_failed=None, activity: str | None = None):
        """Run `fn` on a worker thread; `activity` names it in the status indicator."""
        w = Worker(fn, *args)
        self._workers.add(w)
        token = self.status.begin(activity) if activity else None

        def cleanup():
            self._workers.discard(w)
            if token is not None:
                self.status.end(token)

        w.signals.done.connect(on_done)
        w.signals.done.connect(cleanup)
        w.signals.failed.connect(self._on_error)
        if on_failed is not None:
            w.signals.failed.connect(on_failed)
        w.signals.failed.connect(cleanup)
        self.pool.start(w)

    def _on_error(self, msg: str):
        if self.status.state in (graph.OFFLINE, graph.SIGNIN):
            return  # the indicator already says so; no need to repeat it on the left
        self.statusBar().showMessage(f"Error: {msg}", 10_000)

    def _sync_failed(self, _msg: str):
        self._syncing = False

    # -- startup ------------------------------------------------------
    def _start_initial_sync(self):
        mailboxes = self.mailboxes

        def job():
            mailboxes.ensure_accounts()  # opens the browser on first run
            self._sync_all_folders()
            first = mailboxes.all()[0]
            inbox = self.store.well_known_folder(first.id, "inbox")
            error, new = None, []
            if inbox:
                try:
                    new = first.sync.sync_folder(inbox).new_ids
                except Exception as exc:  # offline: still show what's stored locally
                    log.warning("inbox sync failed: %s", exc)
                    error = str(exc)
            return inbox, error, new

        self._syncing = True
        self._run(job, self._initial_sync_done, on_failed=self._sync_failed,
                  activity="Connecting…")

    def _sync_all_folders(self):
        """Send queued changes and refresh every mailbox's folder list and counts (worker
        thread). One mailbox failing, say a shared one whose access was taken away, doesn't
        stop the others."""
        for box in self.mailboxes.all():
            try:
                box.sync.send_changes()
            except Exception as exc:  # stays queued; mustn't hold up the folder refresh
                log.warning("sending changes for %s failed: %s", box.display_name, exc)
            try:
                box.sync.sync_folders()
            except Exception as exc:
                log.warning("folder sync failed for %s: %s", box.display_name, exc)

    def _initial_sync_done(self, payload):
        inbox_id, error, new = payload
        self._populate_folders()
        if inbox_id:
            self.folder_tree.select_folder(inbox_id)  # the sync this starts is skipped below
        self._syncing = False
        if error:
            self.statusBar().showMessage(f"Couldn't sync, showing stored mail: {error}", 10_000)
        else:
            self.status.synced()
        self.timer.start()
        self._notify_new(new)  # mail that arrived while WaddleMail was closed
        self.contacts_timer.start()
        self.refresh_contacts()
        self.calendar_timer.start()
        self.refresh_calendar(calendars=True)
        self.reminders.start()

    # -- folders ------------------------------------------------------
    def _populate_folders(self):
        self.folder_tree.populate()
        self._update_unread_indicators()
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

        self._run(job, self._mailbox_added, on_failed=self._sync_failed, activity="Signing in…")

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

        self._run(job, self._mailbox_added, on_failed=failed, activity="Opening mailbox…")

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

    # -- folder management --------------------------------------------
    def new_folder(self):
        """New subfolder of the selected folder (File > New > Folder)."""
        item = self.folder_tree.currentItem()
        if item is None:
            self.statusBar().showMessage("Select a folder or mailbox first", 4000)
            return
        if not self.folder_tree.can_hold_folders(item):
            self.statusBar().showMessage(f"{item.text(0)} can't contain folders", 4000)
            return
        self.folder_tree.setFocus()
        self.folder_tree.start_new_folder()

    def _name_problem(self, mailbox_id: str, parent_id: str, name: str,
                      exclude: str | None = None) -> str | None:
        """Why `name` can't be used under `parent_id` ("" = top of the mailbox), if it can't."""
        parent = self.store.folder_row(parent_id) if parent_id else None
        if parent is not None and parent["well_known"] in NO_SUBFOLDERS:
            return f"{parent['name']} can't contain folders."
        if not name:
            return "A folder needs a name."
        if len(name) > 255:
            return "That name is too long."
        rows = self.store.folder_rows(mailbox_id)
        ids = {r["id"] for r in rows}
        for r in rows:
            sibling_parent = r["parent_id"] if r["parent_id"] in ids else ""
            if sibling_parent == parent_id and r["id"] != exclude \
                    and r["name"].casefold() == name.casefold():
                return f"There is already a folder called \"{r['name']}\" there."
        return None

    def _folder_failed(self, title: str, msg: str):
        self._populate_folders()  # put the pane back the way the store has it
        QMessageBox.warning(self, title, msg)

    def _create_folder(self, mailbox_id: str, parent_id: str, name: str):
        box = self.mailboxes.get(mailbox_id)
        problem = self._name_problem(mailbox_id, parent_id, name) if box else "No such mailbox."
        if problem:
            self._folder_failed("New folder", problem)
            return
        self.statusBar().showMessage(f"Creating {name}…")

        def job():
            folder = box.graph.create_folder(name, parent_id or None)
            box.sync.sync_folders()
            return folder["id"]

        def done(folder_id: str):
            self._populate_folders()
            self.folder_tree.select_folder(folder_id)
            self.statusBar().showMessage(f"Created {name}", 3000)

        self._run(job, done, activity="Creating folder…",
                  on_failed=lambda msg: self._folder_failed(
                      "New folder", f"Couldn't create \"{name}\".\n\n{msg}"))

    def _rename_folder(self, folder_id: str, name: str):
        row = self.store.folder_row(folder_id)
        box = self.mailboxes.for_folder(folder_id)
        if row is None or box is None:
            return
        ids = {r["id"] for r in self.store.folder_rows(box.id)}
        parent = row["parent_id"] if row["parent_id"] in ids else ""
        problem = self._name_problem(box.id, parent, name, exclude=folder_id)
        if problem:
            self._folder_failed("Rename folder", problem)
            return

        def job():
            box.graph.rename_folder(folder_id, name)
            box.sync.sync_folders()

        def done(_):
            self._populate_folders()
            self.statusBar().showMessage(f"Renamed to {name}", 3000)

        self._run(job, done, activity="Renaming folder…",
                  on_failed=lambda msg: self._folder_failed(
                      "Rename folder", f"Couldn't rename \"{row['name']}\".\n\n{msg}"))

    def delete_folder(self, folder_id: str):
        """Move a folder to Deleted Items, as Outlook does; one already in Deleted Items is
        deleted for good."""
        row = self.store.folder_row(folder_id)
        box = self.mailboxes.for_folder(folder_id)
        if row is None or box is None or row["well_known"]:
            return
        in_trash = any(r["well_known"] == "deleteditems" for r in self.store.folder_path(folder_id))
        subtree = self.store.folder_subtree(folder_id)
        items = sum(r["total"] or 0 for r in subtree)
        parts = [f"{items} message{'s' if items != 1 else ''}"]
        if len(subtree) > 1:
            n = len(subtree) - 1
            parts.append(f"{n} subfolder{'s' if n != 1 else ''}")
        contents = " and ".join(parts)
        if in_trash:
            title = "Delete folder permanently"
            q = (f"Permanently delete \"{row['name']}\" and its {contents}?\n\n"
                 "This can't be undone.")
        else:
            title = "Delete folder"
            q = f"Move \"{row['name']}\" and its {contents} to Deleted Items?"
        if QMessageBox.question(self, title, q) != QMessageBox.StandardButton.Yes:
            return
        gone = {r["id"] for r in subtree}
        self.statusBar().showMessage(f"Deleting {row['name']}…")

        def job():
            if in_trash:
                box.graph.delete_folder(folder_id)
            else:
                box.graph.move_folder(folder_id, "deleteditems")
            box.sync.sync_folders()

        def done(_):
            if in_trash and self._current_folder in gone:
                # the folder we were looking at no longer exists: go to the Inbox
                self._current_folder = None
                inbox = self.store.well_known_folder(box.id, "inbox")
                self._populate_folders()
                if inbox:
                    self.folder_tree.select_folder(inbox)
            else:
                self._populate_folders()
            self.statusBar().showMessage(f"Deleted {row['name']}", 3000)

        self._run(job, done, activity="Deleting folder…",
                  on_failed=lambda msg: self._folder_failed(
                      title, f"Couldn't delete \"{row['name']}\".\n\n{msg}"))

    # -- message list ---------------------------------------------------
    def _refresh_messages(self):
        if not self._current_folder:
            return
        words = self.filter_bar.search_text()
        hits = self._server_hits.get((self._current_folder, words)) if words else None
        rows = self.store.list_messages(self._current_folder, since=self._list_cutoff(),
                                        also_ids=hits, **self.filter_bar.criteria())
        folder = self.store.folder_row(self._current_folder)
        self.msg_list.folder_id = self._current_folder  # for dragging messages out
        self.msg_list.set_messages(rows, bool(folder) and folder["well_known"] in OUTGOING)
        self._update_message_actions()
        self._update_older_bar()
        if not words:
            self._search_note = ""
            self._search_timer.stop()
        elif hits is None and self.settings.get("search/server"):
            self._search_note = "searching server…"
            # restart the wait only for new words, not for every refresh (syncs, read marks)
            query = (self._current_folder, words)
            if not self._search_timer.isActive() or self._pending_search != query:
                self._pending_search = query
                self._search_timer.start()
        self.filter_bar.set_count(len(rows), self._search_note if words else "")

    def _search_server(self):
        """Ask the server for matches in the current folder, including mail whose body was
        never downloaded or that is older than the sync window. Hits are stored like synced
        mail, then shown alongside the local matches."""
        fid, words = self._current_folder, self.filter_bar.search_text()
        box = self.mailboxes.for_folder(fid) if fid else None
        if box is None or not words:
            return
        self._search_gen += 1
        gen = self._search_gen

        def job():
            items = box.graph.search_messages(fid, words.split())
            self.store.apply_message_changes(fid, items)
            return gen, fid, words, [i["id"] for i in items]

        def done(payload):
            gen_, fid_, words_, ids = payload
            self._server_hits[(fid_, words_)] = ids
            if gen_ == self._search_gen:
                self._search_note = f"{len(ids)} on server" if ids else "none on server"
                self._refresh_messages()

        def failed(msg: str):
            if gen == self._search_gen:
                self._search_note = "server search unavailable"
                self.filter_bar.set_count(len(self._listed_ids()), self._search_note)

        self._run(job, done, on_failed=failed, activity="Searching server…")

    def _listed_ids(self) -> list[str]:
        return [m["id"] for m in self.msg_list.model_.root.messages()]

    def sync_current(self):
        fid = self._current_folder
        box = self.mailboxes.for_folder(fid) if fid else None
        if box is None or self._syncing:
            return
        self._syncing = True

        max_age_days = self.settings.get("cache/max_age_days")  # read on the UI thread

        def job():
            res = box.sync.sync_folder(fid)
            try:  # sizes come separately (one cheap request, only when some are missing)
                box.sync.fill_sizes(fid)
            except Exception as exc:
                log.info("message sizes not fetched: %s", exc)
            new = list(res.new_ids)
            # Every account's Inbox too, so new mail is noticed (and announced) wherever
            # you are. Shared mailboxes only get their counts refreshed.
            for other in self.mailboxes.all():
                inbox = None if other.shared else self.store.well_known_folder(other.id, "inbox")
                if inbox and inbox != fid:
                    try:
                        new += other.sync.sync_folder(inbox).new_ids
                    except Exception as exc:
                        log.warning("inbox sync failed for %s: %s", other.display_name, exc)
            self._sync_all_folders()  # refresh unread counts / new folders everywhere
            if max_age_days:
                self.store.purge_bodies_older_than(max_age_days)
            return fid, res, new

        self._run(job, self._sync_done, on_failed=self._sync_failed,
                  activity="Syncing with server…")

    def _sync_done(self, payload):
        self._syncing = False
        fid, res, new = payload
        for box in self.mailboxes.all():  # say it (once) if the server won't give these
            refused = [what for what, why in (("replied/forwarded marks",
                                               box.sync.verbs_refused),
                                              ("message sizes", box.sync.sizes_refused)) if why]
            if refused and not getattr(box, "_told_refused", False):
                box._told_refused = True
                self.statusBar().showMessage(
                    f"The server didn't send {' or '.join(refused)} for {box.display_name}",
                    15_000)
        self.status.synced()
        self._notify_new(new)
        if res.upserted or res.removed:  # new mail may match a search: ask the server again
            self._server_hits = {k: v for k, v in self._server_hits.items() if k[0] != fid}
        self._populate_folders()
        if fid == self._current_folder:
            self._refresh_messages()

    def _on_message_selected(self, *_):
        self._update_message_actions()
        self._read_timer.stop()
        # One row (a message, or a conversation: its newest message) shows in the pane.
        shown = self.msg_list.shown_id()
        if shown is None:
            self._current_message = None
            n = len(self.msg_list.selected_ids())
            self.msg_view.show_placeholder(
                f"{n} messages selected." if n else "Select a message.")
            return
        if shown == self._current_message:
            return
        self._current_message = shown
        if not self.msg_view.isHidden():  # reading pane is on
            self._show_in_pane(shown)

    def _show_in_pane(self, mid: str):
        row = self.store.message_row(mid)
        box = self.mailboxes.for_folder(self._current_folder)
        if row is None or box is None:
            return
        self.msg_view.show_message(row)
        self._load_attachments(box, row, lambda items: self.msg_view.set_attachments(mid, items))
        self._run(lambda: (mid, box.bodies.get(mid)), self._body_ready,
                  on_failed=lambda msg: self._body_failed(mid, msg),
                  activity=None if self.store.has_body(mid) else "Downloading message…")
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
        win = MessageWindow(row, self.images_action.isChecked(), [self.act_delete],
                            lambda copy: self.move_messages([mid], copy=copy, parent=win), self,
                            respond=lambda kind: self.respond(kind, mid))
        self._windows.add(win)
        win.destroyed.connect(lambda *_: self._windows.discard(win))
        self._connect_attachments(win.view)
        win.show()
        self._load_attachments(box, row, win.set_attachments)
        self._run(lambda: box.bodies.get(mid),
                  lambda body: win.set_body(body.content_type, body.content),
                  activity=None if self.store.has_body(mid) else "Downloading message…",
                  on_failed=win.show_error)
        self.mark_read([mid], True)

    # -- received attachments -------------------------------------------------
    def _connect_attachments(self, view: MessageView):
        view.attachments.open_requested.connect(self.open_attachments)
        view.attachments.save_requested.connect(self.save_attachments)

    def _load_attachments(self, box, row, show):
        """List a message's attachments (not the pictures in its body) for `show`."""
        if not row["has_attachments"]:
            return
        item = box.graph.message_path(row["id"])
        self._run(lambda: [a for a in box.graph.attachments(item) if not a.get("isInline")],
                  show)

    def _download(self, mid: str, items: list[dict], folder: str, paths=None):
        """A job writing attachments into `folder` (or to `paths`); returns the paths."""
        row = self.store.message_row(mid)
        box = self.mailboxes.for_folder(row["folder_id"]) if row else None
        if box is None:
            return None
        item = box.graph.message_path(mid)

        def job():
            out = []
            for i, att in enumerate(items):
                path = paths[i] if paths else unique_path(folder, file_name(att))
                data = box.graph.attachment_content(item, att["id"])
                with open(path, "wb") as fh:
                    fh.write(data)
                out.append(path)
            return out
        return job

    def open_attachments(self, mid: str, items: list[dict]):
        """Download to a private temporary folder and open with the desktop's app."""
        job = self._download(mid, items, tempfile.mkdtemp(prefix="waddlemail-"))
        if job is None:
            return

        def done(paths):
            for path in paths:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        self._run(job, done, on_failed=lambda msg: QMessageBox.warning(
            self, "Open attachment", f"The attachment couldn't be opened.\n\n{msg}"),
                  activity="Downloading attachment…")

    def save_attachments(self, mid: str, items: list[dict]):
        """One: Save as; several: into a folder (names already there get " (2)")."""
        parent = QApplication.activeWindow() or self
        start = self._save_dir or QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation) or os.path.expanduser("~")
        if len(items) == 1:
            path, _ = QFileDialog.getSaveFileName(parent, "Save attachment",
                                                  os.path.join(start, file_name(items[0])))
            if not path:
                return
            folder, paths = os.path.dirname(path), [path]
        else:
            folder = QFileDialog.getExistingDirectory(parent, "Save attachments to", start)
            if not folder:
                return
            paths = None
        self._save_dir = folder
        job = self._download(mid, items, folder, paths)
        if job is None:
            return

        def done(saved):
            what = os.path.basename(saved[0]) if len(saved) == 1 else f"{len(saved)} attachments"
            self.statusBar().showMessage(f"Saved {what} to {folder}", 5000)

        self._run(job, done, on_failed=lambda msg: QMessageBox.warning(
            parent, "Save attachment", f"The attachment wasn't saved.\n\n{msg}"),
                  activity="Saving attachment…")

    # -- older mail ---------------------------------------------------------
    def _update_older_bar(self):
        fid = self._current_folder
        folder = self.store.folder_row(fid) if fid else None
        if folder is None or fid in self._no_older or self.filter_bar.active():
            self.older_bar.hide()
            return
        missing = (folder["total"] or 0) - self.store.message_count(fid)
        self.older_bar.show_missing(missing, busy=fid in self._downloading)

    def download_older(self):
        """Fetch the next 500 messages older than what this folder has stored."""
        fid = self._current_folder
        box = self.mailboxes.for_folder(fid) if fid else None
        if box is None or fid in self._downloading:
            return
        self._downloading.add(fid)
        self._update_older_bar()

        def done(count: int):
            self._downloading.discard(fid)
            if count == 0:  # the rest aren't messages we can list (or are already here)
                self._no_older.add(fid)
            if fid == self._current_folder:
                self._refresh_messages()
            self.statusBar().showMessage(
                f"Downloaded {count} older message{'s' if count != 1 else ''}", 4000)

        def failed(_msg: str):
            self._downloading.discard(fid)
            self._update_older_bar()

        self._run(lambda: box.sync.fetch_older(fid), done, on_failed=failed,
                  activity="Downloading older mail…")

    # -- contacts -----------------------------------------------------------
    _NEW = {"mail": ("New Mail", "mail-plus", "New mail message (Ctrl+N)"),
            "calendar": ("New Event", "calendar-plus", "New event (Ctrl+Shift+E)"),
            "contacts": ("New Contact", "user-plus", "New contact (Ctrl+Shift+C)")}

    def _update_new_button(self, page: str):
        text, icon, tip = self._NEW[page]
        self.new_btn.setText(text)
        self.new_btn.setIcon(iconset.load(icon))
        self.new_btn.setToolTip(f"{tip}; the arrow offers the others")
        self._new_page = page

    def _new_for_page(self):
        {"calendar": self.new_event, "contacts": self.new_contact}.get(
            self._new_page, self.new_mail)()

    def new_contact(self):
        self.contacts_view.new_contact()

    def show_page(self, page: str):
        """"mail", "calendar" or "contacts"."""
        widget = {"contacts": self.contacts_view, "calendar": self.calendar_page}.get(
            page, self.outer)
        self.pages.setCurrentWidget(widget)
        self._nav[page].setChecked(True)
        self._update_new_button(page)
        if page == "contacts":
            self.contacts_view.search.setFocus()
        elif page == "calendar":
            self.calendar_page.grid.setFocus()

    def refresh_contacts(self):
        """Download contacts, contact groups and the directory for every account, and
        recount the people you write to (from Sent Items, synced here too)."""
        if self._contacts_busy:
            return
        boxes = [b for b in self.mailboxes.all() if b.contacts is not None]
        if not boxes:
            return
        self._contacts_busy = True

        def job():
            problems = []
            for box in boxes:
                res = box.contacts.refresh()
                problems += [f"{box.display_name}: {p}" for p in res.problems]
                if res.groups is None:
                    problems.append(f"{box.display_name}: contact groups are unavailable "
                                    "(Microsoft offers them only in a preview API).")
                sent = self.store.well_known_folder(box.id, "sentitems")
                if sent:
                    try:
                        box.sync.sync_folder(sent)
                    except Exception as exc:
                        log.warning("sent items sync failed: %s", exc)
            self.store.rebuild_recipients()
            return problems

        def done(problems: list[str]):
            self._contacts_busy = False
            self.contacts_view.reload()
            self.contacts_view.show_problems(sorted(set(problems)))

        def failed(_msg: str):
            self._contacts_busy = False

        self._run(job, done, on_failed=failed, activity="Updating contacts…")

    def _contacts_box(self, account_id: str | None = None):
        own = [b for b in self.mailboxes.all() if b.contacts is not None]
        return next((b for b in own if b.account_id == account_id), own[0] if own else None)

    def save_contact(self, fields: dict, contact_id: str, folder_id: str, folder_name: str,
                     photo: bytes | None = None):
        row = self.store.contact_row(contact_id) if contact_id else None
        box = self._contacts_box(row["account_id"] if row else None)
        if box is None:
            return

        def job():
            item = box.graph.save_contact(fields, contact_id or None, folder_id or None)
            self.store.upsert_contact(personal_row(item, box.account_id, folder_id, folder_name))
            if photo:
                box.graph.set_contact_photo(item["id"], photo, folder_id or None)
                self.store.set_photo(item["id"], photo)
            return item["id"]

        def done(new_id: str):
            self.contacts_view.reload()
            self.contacts_view.select_contact(new_id)
            self.statusBar().showMessage("Contact saved", 3000)

        self._run(job, done, activity="Saving contact…", on_failed=lambda msg: QMessageBox.warning(
            self, "Contacts", f"Couldn't save the contact.\n\n{msg}"))

    def delete_contact(self, contact_id: str):
        row = self.store.contact_row(contact_id)
        box = self._contacts_box(row["account_id"] if row else None)
        if row is None or box is None:
            return
        if QMessageBox.question(self, "Delete contact",
                                f"Delete {row['display_name']} from your contacts?") \
                != QMessageBox.StandardButton.Yes:
            return

        def job():
            box.graph.delete_contact(contact_id)
            self.store.delete_contact(contact_id)

        def done(_):
            self.contacts_view.reload()
            self.statusBar().showMessage("Contact deleted", 3000)

        self._run(job, done, activity="Deleting contact…",
                  on_failed=lambda msg: QMessageBox.warning(
                      self, "Contacts", f"Couldn't delete the contact.\n\n{msg}"))

    def fetch_photo(self, contact_id: str, folder_id: str):
        """Download a contact's (or directory person's) photo once; the view redraws."""
        row = self.store.contact_row(contact_id)
        box = self._contacts_box(row["account_id"] if row else None)
        if box is None or contact_id in self._photos_pending:
            return
        self._photos_pending.add(contact_id)

        def done(_):
            self._photos_pending.discard(contact_id)
            self.contacts_view.photo_arrived(contact_id)

        def failed(_msg):
            self._photos_pending.discard(contact_id)

        self._run(lambda: box.contacts.photo(contact_id, folder_id or None), done,
                  on_failed=failed)

    def save_group(self, group_id: str, name: str, notes: str, add: list, remove: list):
        """Create a contact group (no `group_id`) or change one: name, notes, members to add
        (addMembers payloads) and stored members to remove."""
        g = self.store.group_row(group_id) if group_id else None
        box = self._contacts_box(g["account_id"] if g else None)
        if box is None:
            return

        def job():
            gid = group_id
            if not gid:
                gid = box.graph.create_group(name, notes)["id"]
            elif (name, notes) != (g["name"], g["notes"] or ""):
                box.graph.update_group(gid, name, notes)
            box.graph.remove_group_members(gid, [removal_payload(m) for m in remove])
            box.graph.add_group_members(gid, add)
            box.contacts.sync_groups()  # read back members as the server now has them
            return gid

        def done(gid: str):
            self.contacts_view.reload()
            self.contacts_view.show_source("groups")
            self.contacts_view.select_key(("group", gid))
            self.statusBar().showMessage(f"Saved group {name}", 3000)

        self._run(job, done, activity="Saving contact group…",
                  on_failed=lambda msg: self._group_failed(f"Couldn't save {name}.\n\n{msg}"))

    def delete_group(self, group_id: str):
        g = self.store.group_row(group_id)
        box = self._contacts_box(g["account_id"] if g else None)
        if g is None or box is None:
            return
        if QMessageBox.question(self, "Delete contact group",
                                f"Delete the group {g['name']}?\n\nThe people in it stay in "
                                "your contacts.") != QMessageBox.StandardButton.Yes:
            return

        def job():
            box.graph.delete_group(group_id)
            self.store.delete_group(group_id)

        def done(_):
            self.contacts_view.reload()
            self.statusBar().showMessage(f"Deleted group {g['name']}", 3000)

        self._run(job, done, activity="Deleting contact group…",
                  on_failed=lambda msg: self._group_failed(
                      f"Couldn't delete {g['name']}.\n\n{msg}"))

    def _group_failed(self, text: str):
        self.contacts_view.reload()
        QMessageBox.warning(self, "Contact groups",
                            text + "\n\n(Contact groups use Microsoft's preview API.)")

    # -- move / copy --------------------------------------------------------
    def _pick_folder(self, mailbox_id: str, title: str, prompt: str, parent=None,
                     **kwargs) -> str | None:
        dlg = FolderPicker(self.store, mailbox_id, title, prompt,
                           preselect=self._last_dest.get(mailbox_id), parent=parent or self,
                           **kwargs)
        if not dlg.exec() or dlg.chosen is None:
            return None
        return dlg.chosen

    def move_messages(self, ids: list[str], dest: str | None = None, copy: bool = False,
                      parent=None):
        """Move (or copy) messages to folder `dest`; without `dest`, ask which folder.
        A move shows at once and is sent through the change queue; a copy needs the server,
        which makes the new message."""
        rows = [r for r in (self.store.message_row(i) for i in ids) if r is not None]
        if not rows:
            return
        source = rows[0]["folder_id"]
        box = self.mailboxes.for_folder(source)
        if box is None:
            return
        ids = [r["id"] for r in rows]
        what = "1 message" if len(ids) == 1 else f"{len(ids)} messages"
        if dest is None:
            verb = "Copy" if copy else "Move"
            dest = self._pick_folder(box.id, f"{verb} to folder", f"{verb} {what} to:",
                                     parent=parent,
                                     disabled=frozenset() if copy else frozenset({source}))
            if dest is None:
                return
        if self.store.folder_mailbox(dest) != box.id:
            QMessageBox.warning(parent or self, "Move to folder",
                                "Messages can only be moved or copied within one mailbox.")
            return
        self._last_dest[box.id] = dest
        name = (self.store.folder_row(dest) or {"name": "folder"})["name"]
        if copy:
            self.statusBar().showMessage(f"Copying {what} to {name}…")

            def job():
                items = [box.graph.copy_message(i, dest) for i in ids]
                self.store.apply_message_changes(dest, items)
                box.sync.sync_folders()

            def done(_):
                self._counts_changed()
                if dest == self._current_folder:
                    self._refresh_messages()
                self.statusBar().showMessage(f"Copied {what} to {name}", 4000)

            self._run(job, done, activity="Copying messages…",
                      on_failed=lambda msg: QMessageBox.warning(
                          parent or self, "Copy to folder", f"Couldn't copy {what}.\n\n{msg}"))
            return
        follow = self.msg_list.neighbor_after(set(ids))  # Outlook moves on to the next one
        moved = self.store.move_messages(box.id, ids, dest)
        if not moved:
            return
        if source == self._current_folder:
            selected = set(self.msg_list.selected_ids())
            self._refresh_messages()
            if selected & set(moved) and follow:
                self.msg_list.select_id(follow)
        self._counts_changed()
        self._send_timer.start()
        self.statusBar().showMessage(f"Moved {what} to {name}", 4000)

    def pick_move_folder(self, folder_id: str):
        row = self.store.folder_row(folder_id)
        box = self.mailboxes.for_folder(folder_id)
        if row is None or box is None:
            return
        ids = {r["id"] for r in self.store.folder_rows(box.id)}
        parent = row["parent_id"] if row["parent_id"] in ids else TOP
        no_room = {r["id"] for r in self.store.folder_rows(box.id)
                   if r["well_known"] in NO_SUBFOLDERS}
        dest = self._pick_folder(box.id, "Move folder", f"Move \"{row['name']}\" to:",
                                 subtree=folder_id, allow_top=parent != TOP,
                                 disabled=frozenset({parent} | no_room))
        if dest is not None:
            self.move_folder(folder_id, dest)

    def move_folder(self, folder_id: str, dest: str):
        """Move a folder (and everything in it) under folder `dest`, or to the top of its
        mailbox when `dest` is ""."""
        row = self.store.folder_row(folder_id)
        box = self.mailboxes.for_folder(folder_id)
        if row is None or box is None or row["well_known"]:
            return
        if dest and dest in {r["id"] for r in self.store.folder_subtree(folder_id)}:
            return
        problem = self._name_problem(box.id, dest, row["name"], exclude=folder_id)
        if problem:
            self._folder_failed("Move folder", problem)
            return
        target = (self.store.folder_row(dest)["name"] if dest
                  else box.display_name)
        self.statusBar().showMessage(f"Moving {row['name']}…")

        def job():
            moved = box.graph.move_folder(folder_id, dest or "msgfolderroot") or {}
            box.sync.sync_folders()
            return moved.get("id") or folder_id

        def done(new_id: str):
            was_current = self._current_folder == folder_id
            self._populate_folders()
            if was_current and new_id != folder_id:  # Exchange gave it a new id
                self.folder_tree.select_folder(new_id)
            self.statusBar().showMessage(f"Moved {row['name']} to {target}", 4000)

        self._run(job, done, activity="Moving folder…",
                  on_failed=lambda msg: self._folder_failed(
                      "Move folder", f"Couldn't move \"{row['name']}\".\n\n{msg}"))

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
        self._counts_changed()
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

        self._run(job, self._changes_sent, on_failed=self._sync_failed,
                  activity="Sending changes…")

    def _changes_sent(self, _):
        self._syncing = False
        self.status.refresh()  # fewer changes waiting
        # A change the server refused was undone in the store; show that.
        self._refresh_messages()
        self._counts_changed()

    # -- tray, notifications, badge ---------------------------------------
    def _counts_changed(self):
        self.folder_tree.refresh_counts()
        self._update_unread_indicators()
        self.status.refresh()  # read/flag/move changes waiting to be sent

    def _own_inboxes(self) -> list[tuple[str, str]]:
        """(mailbox display name, inbox folder id) for each signed-in account."""
        out = []
        for box in self.mailboxes.all():
            inbox = None if box.shared else self.store.well_known_folder(box.id, "inbox")
            if inbox:
                out.append((box.display_name, inbox))
        return out

    def _update_unread_indicators(self):
        """Unread mail in the accounts' Inboxes: the tray's dot and the task manager badge."""
        total = sum((self.store.folder_row(fid) or {"unread": 0})["unread"] or 0
                    for _, fid in self._own_inboxes())
        self.tray.set_unread(total)
        self.notifier.set_badge(total)

    def _notify_new(self, ids: list[str]):
        """A notification per account for new, unread Inbox mail from the last day (older
        "new" mail is a catch-up after a long time offline, not news)."""
        if not ids or not self.settings.get("notify/new_mail"):
            return
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for mailbox, inbox in self._own_inboxes():
            if inbox == self._current_folder and self.isActiveWindow():
                continue  # it's in front of you already
            rows = [r for r in (self.store.message_row(i) for i in ids)
                    if r and r["folder_id"] == inbox and not r["is_read"]
                    and (r["received"] or "") >= cutoff]
            if not rows:
                continue
            rows.sort(key=lambda r: r["received"] or "", reverse=True)
            if len(rows) == 1:
                r = rows[0]
                summary = r["sender_name"] or r["sender_addr"] or "New message"
                body = (r["subject"] or "(no subject)") + (
                    "\n" + r["preview"][:140] if r["preview"] else "")
                key = f"{inbox}|{r['id']}"
            else:
                summary = f"{len(rows)} new messages"
                lines = [f"{r['sender_name'] or r['sender_addr']}: {r['subject'] or '(no subject)'}"
                         for r in rows[:4]]
                if len(rows) > 4:
                    lines.append(f"…and {len(rows) - 4} more")
                body = "\n".join(lines)
                key = f"{inbox}|"
            self.notifier.notify(summary, body, key, origin=mailbox)

    def _open_from_notification(self, key: str):
        if key.startswith("event|"):  # a reminder: show the reminders
            self.reminder_window.show()
            self.reminder_window.raise_()
            self.reminder_window.activateWindow()
            return
        self.tray.show_window()
        folder, _, message = key.partition("|")
        if folder:
            self.folder_tree.select_folder(folder)
        if message:
            self.msg_list.select_id(message)

    def close_to_tray(self) -> bool:
        return (self.settings.get("tray/close_to_tray") and self.tray.isVisible())

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.quit()

    def _toggle_launcher(self):
        try:
            if desktop.is_installed():
                desktop.uninstall()
                self.statusBar().showMessage("Removed from the application menu", 4000)
            else:
                desktop.install()
                self.statusBar().showMessage("Added to the application menu", 4000)
        except OSError as exc:
            QMessageBox.warning(self, "Application menu", f"That didn't work: {exc}")
        self.act_launcher.setText(self._launcher_label())

    @staticmethod
    def _launcher_label() -> str:
        return ("Remove from &application menu" if desktop.is_installed()
                else "Add to &application menu")

    def _update_message_actions(self, *_):
        any_selected = bool(self.msg_list.selected_ids())
        for act in self._message_actions:
            act.setEnabled(any_selected)
        one = self.msg_list.shown_id() is not None  # reply to one message (or conversation)
        for act in (self.act_reply, self.act_reply_all, self.act_forward):
            act.setEnabled(one)

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
        self._apply_list_options()
        self.act_filter_bar.setChecked(s.get("view/filter_bar"))
        self.filter_bar.setVisible(s.get("view/filter_bar"))
        self.set_reading_pane(s.get("view/reading_pane"))

    def closeEvent(self, event):
        if not self._quitting and self.close_to_tray():
            event.ignore()
            self.hide()  # keeps syncing and notifying; the tray brings it back
            return
        self.notifier.close()
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
        s.set("list/threads", self.act_threads.isChecked())
        s.set("list/date_groups", self.act_date_groups.isChecked())
        for win in list(self._windows):
            win.close()
        super().closeEvent(event)

    def _apply_list_options(self, *_):
        self.msg_list.set_options(self.act_threads.isChecked(), self.act_date_groups.isChecked())

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
        self.new_menu.addAction(self._themed(
            "&Contact…", "user-plus", self.new_contact, "Ctrl+Shift+C"))
        self.new_menu.addSeparator()
        self.new_menu.addAction(self._themed("&Folder…", "folder-plus", self.new_folder))

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
        self.act_reply = self._themed("Reply", "reply", lambda: self.respond("reply"),
                                      "Ctrl+R", enabled=False, tip="Reply (Ctrl+R)")
        self.act_reply_all = self._themed("Reply all", "reply-all",
                                          lambda: self.respond("replyAll"), "Ctrl+Shift+R",
                                          enabled=False, tip="Reply all (Ctrl+Shift+R)")
        self.act_forward = self._themed("Forward", "forward", lambda: self.respond("forward"),
                                        "Ctrl+F", enabled=False, tip="Forward (Ctrl+F)")
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
        self.msg_list.addAction(self._separator())
        self.act_move = self._list_action(
            "Mo&ve to folder…", lambda: self.move_messages(sel()), "Ctrl+Shift+V",
            icon="folder-input")
        self.act_copy_to = self._list_action(
            "Cop&y to folder…", lambda: self.move_messages(sel(), copy=True), icon="copy")
        self._message_actions = [self.act_open, self.act_mark_read, self.act_mark_unread,
                                 self.act_toggle_read, self.act_toggle_flag,
                                 self.act_flag_complete, self.act_move, self.act_copy_to]

        self.act_threads = QAction("Group by &conversation", self)
        self.act_threads.setCheckable(True)
        self.act_threads.setChecked(self.settings.get("list/threads"))
        self.act_date_groups = QAction("Show &date headings", self)
        self.act_date_groups.setCheckable(True)
        self.act_date_groups.setChecked(self.settings.get("list/date_groups"))
        self.act_date_groups.setToolTip("Today, Yesterday, Last week, ... (when sorted by date)")
        for act in (self.act_threads, self.act_date_groups):
            act.toggled.connect(self._apply_list_options)
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

    def _build_nav(self):
        """The rail on the far left: Mail / Contacts (Ctrl+1 / Ctrl+2)."""
        rail = QToolBar("Navigation", self)
        rail.setObjectName("nav-rail")
        rail.setMovable(False)
        rail.setOrientation(Qt.Orientation.Vertical)
        rail.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        rail.setIconSize(rail.iconSize() * 1.2)
        group = QActionGroup(self)
        self._nav: dict[str, QAction] = {}
        for page, label, icon, key in (("mail", "Mail", "mail", "Ctrl+1"),
                                       ("calendar", "Calendar", "calendar", "Ctrl+2"),
                                       ("contacts", "Contacts", "contact", "Ctrl+3")):
            act = self._themed(label, icon, lambda p=page: self.show_page(p), key,
                               tip=f"{label} ({key})")
            act.setCheckable(True)
            group.addAction(act)
            rail.addAction(act)
            self._nav[page] = act
        self._nav["mail"].setChecked(True)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, rail)

    def _build_toolbar(self):
        bar = QToolBar("Main", self)
        bar.setObjectName("main-toolbar")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(bar)
        self.toolbar = bar

        # The button makes what belongs to the page you're on (New Mail / New Event /
        # New Contact); its arrow still offers everything.
        self.new_btn = new_btn = QToolButton()
        new_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        new_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        new_btn.setMenu(self.new_menu)
        new_btn.clicked.connect(lambda checked=False: self._new_for_page())
        bar.addWidget(new_btn)
        self._update_new_button("mail")
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
        file_menu.addAction(self._action("&Quit", self.quit_app, QKeySequence.StandardKey.Quit))

        edit_menu = mb.addMenu("&Edit")
        for act in (self.act_cut, self.act_copy, self.act_paste):
            edit_menu.addAction(act)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_select_all)
        edit_menu.addAction(self.act_focus_filter)
        edit_menu.addSeparator()
        edit_menu.addAction(self._action("&Preferences…", self.show_preferences, "Ctrl+,"))

        view_menu = mb.addMenu("&View")
        view_menu.addAction(self._nav["mail"])
        view_menu.addAction(self._nav["calendar"])
        view_menu.addAction(self._nav["contacts"])
        view_menu.addSeparator()
        pane_menu = view_menu.addMenu("&Reading pane")
        for mode, _label in READING_PANE_MODES:
            pane_menu.addAction(self._pane_actions[mode])
        view_menu.addAction(self.act_threads)
        view_menu.addAction(self.act_date_groups)
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
        msg_menu.addSeparator()
        msg_menu.addAction(self.act_move)
        msg_menu.addAction(self.act_copy_to)

        help_menu = mb.addMenu("&Help")
        self.act_launcher = self._action(self._launcher_label(), self._toggle_launcher)
        self.act_launcher.setToolTip("Puts WaddleMail and its icon in your desktop's "
                                     "application menu (for this user only)")
        if sys.platform.startswith("linux"):
            help_menu.addAction(self.act_launcher)
            help_menu.addSeparator()
        help_menu.addAction(self._action("&About WaddleMail", self.show_about))

    def show_about(self):
        QMessageBox.about(
            self, "About WaddleMail",
            f"WaddleMail {__version__}\n\nA Linux mail client for Microsoft 365.")

    # -- composing and sending -------------------------------------------------------
    def _compose_accounts(self) -> list[tuple[str, str]]:
        return [(b.account_id, b.display_name) for b in self.mailboxes.all() if not b.shared]

    def _compose(self, **kwargs) -> ComposeWindow:
        s = self.settings
        win = ComposeWindow(self._compose_accounts(), suggestions(self.store),
                            {"html": s.get("compose/html"), "font": s.get("compose/font"),
                             "size": s.get("compose/size"), "color": s.get("compose/color")},
                            self.store.signature, parent=self, **kwargs)
        win.sendRequested.connect(self.send_message)
        win.draftRequested.connect(self.save_draft)
        self._windows.add(win)
        win.destroyed.connect(lambda *_: self._windows.discard(win))
        win.show()
        return win

    def new_mail(self, to: list[str] | None = None):
        if not self._compose_accounts():
            self.statusBar().showMessage("Sign in first", 4000)
            return
        box = self.mailboxes.for_folder(self._current_folder) if self._current_folder else None
        self._compose(to=to, account_id=box.account_id if box else None)

    def respond(self, kind: str, message_id: str | None = None):
        """Reply (kind "reply"), reply all ("replyAll") or forward ("forward") the message
        shown (or `message_id`), with its text quoted below."""
        mid = message_id or self.msg_list.current_id() or self._current_message
        row = self.store.message_row(mid) if mid else None
        box = self.mailboxes.for_folder(row["folder_id"]) if row else None
        if row is None or box is None:
            return
        def job():
            body = box.bodies.get(mid)
            originals: list | None = []
            if kind == "forward" and row["has_attachments"]:
                try:  # shown in the forward, so some can be taken out
                    originals = [Attachment.from_server(a, original=True) for a in
                                 box.graph.attachments(box.graph.message_path(mid))
                                 if not a.get("isInline")]
                except Exception as exc:  # can't list them: they all go along, unshown
                    log.warning("original attachments not listed: %s", exc)
                    originals = None
            return body, originals

        def done(result):
            body, originals = result
            self._compose(kind=kind, original=row,
                          original_body=(body.content_type, body.content),
                          account_id=box.account_id, original_attachments=originals)

        self._run(job, done,
                  activity=None if self.store.has_body(mid) else "Downloading message…")

    def _sending_box(self, win: ComposeWindow):
        """A reply goes out from the mailbox its message is in; new mail from From."""
        if win.original is not None:
            return self.mailboxes.for_folder(win.original["folder_id"])
        return next((b for b in self.mailboxes.all()
                     if not b.shared and b.account_id == win.account_id()), None)

    def _write_draft(self, box, win: ComposeWindow, out) -> str:
        """Create or update the message `out` (win.outgoing(), taken on the GUI thread) in
        Drafts (worker thread); returns its id."""
        message = out.graph_message()
        draft_id = win.draft_id
        if draft_id is None and win.original is None:
            draft_id = box.graph.create_draft(message)["id"]
        else:
            if draft_id is None:
                draft_id = box.graph.create_response(win.original["id"], win.kind)["id"]
            box.graph.update_message(draft_id, message)
        # attachments: upload the new, remove the ones taken out (a forward's included)
        if win.has_had_attachments:
            reconcile(box.graph, box.graph.message_path(draft_id), win.attachments.items(),
                      remove_others=win.originals_known)
        # pictures in the body: inline attachments its cid: links point to
        if out.inline or win.had_pictures:
            ours = {i.content_id.lower() for i in out.inline}
            reconcile_inline(box.graph, box.graph.message_path(draft_id), out.inline,
                             keep_others=bool(cid_refs(out.body) - ours))
            win.had_pictures = True
        return draft_id

    def send_message(self, win: ComposeWindow):
        box = self._sending_box(win)
        if box is None:
            return
        out = win.outgoing()
        win.act_send.setEnabled(False)

        def job():
            if win.original is None and win.draft_id is None and not win.attachments.items() \
                    and not out.inline:
                box.graph.send_mail(out.graph_message())  # plain new mail: straight out
            else:  # attachments, pictures, replies/forwards (threading), saved drafts: a draft
                box.graph.send_draft(self._write_draft(box, win, out))

        original = win.original["id"] if win.original is not None else None
        verb = {"reply": 102, "replyAll": 103, "forward": 104}.get(win.kind)

        def done(_):
            win.sent = True
            win.close()
            self.statusBar().showMessage(f"Sent: {out.subject or '(no subject)'}", 5000)
            if original and verb:  # replied / forwarded mark on the original, right away
                self.store.mark_responded(original, verb)
                self.msg_list.model_.update_rows([self.store.message_row(original)])
            QTimer.singleShot(5000, self.sync_current)  # the reply mark, Sent Items

        def failed(msg: str):
            win.act_send.setEnabled(True)
            QMessageBox.warning(win, "Send", f"The message wasn't sent.\n\n{msg}")

        self._run(job, done, on_failed=failed, activity="Sending…")

    def save_draft(self, win: ComposeWindow):
        box = self._sending_box(win)
        if box is None:
            return
        closing = win.sent  # "Save draft" from closing the window

        def done(draft_id: str):
            win.mark_saved(draft_id)
            self.statusBar().showMessage("Saved to Drafts", 3000)
            if closing:
                win.close()

        def failed(msg: str):
            win.sent = False
            QMessageBox.warning(win, "Save draft", f"The draft wasn't saved.\n\n{msg}")

        out = win.outgoing()
        self._run(lambda: self._write_draft(box, win, out), done, on_failed=failed,
                  activity="Saving draft…")

    # -- calendar ------------------------------------------------------------
    def _calendar_boxes(self):
        return [b for b in self.mailboxes.all() if b.calendar is not None]

    def _calendar_box(self, account_id: str | None = None):
        boxes = self._calendar_boxes()
        return next((b for b in boxes if b.account_id == account_id),
                    boxes[0] if boxes else None)

    def refresh_calendar(self, calendars: bool = False):
        """Fetch the events of the period the Calendar page shows (and, with `calendars`, the
        list of calendars and categories first)."""
        boxes = self._calendar_boxes()
        if not boxes:
            return
        if self._calendar_busy:  # fetch again once this one is done
            self._calendar_fetch_soon.start()
            return
        self._calendar_busy = True
        first, last = self.calendar_page.period()

        def job():
            for box in boxes:
                if calendars or not any(c["account_id"] == box.account_id
                                        for c in self.store.calendar_rows()):
                    box.calendar.sync_calendars()
                box.calendar.fetch(first, last)
                if not (first <= today and last >= ahead):  # keep the coming week for reminders
                    box.calendar.fetch(today - dt.timedelta(days=1), ahead)

        today = dt.date.today()
        ahead = today + dt.timedelta(days=ReminderService.WINDOW_DAYS)

        def done(_):
            self._calendar_busy = False
            self.reminders.check()
            if calendars:
                self.calendar_page.reload_calendars()
            self.calendar_page.reload()
            if self.calendar_page.period() != (first, last):  # moved on meanwhile
                self._calendar_fetch_soon.start()

        def failed(_msg):
            self._calendar_busy = False

        self._run(job, done, on_failed=failed, activity="Updating calendar…")

    @staticmethod
    def _time_zone() -> str:
        """This computer's time zone by its Windows name (what Exchange understands best),
        so repeating events keep their wall-clock time across daylight saving changes."""
        from PySide6.QtCore import QTimeZone
        iana = QTimeZone.systemTimeZoneId()  # (a QByteArray, which the next call wants)
        windows = bytes(QTimeZone.ianaIdToWindowsId(iana)).decode() if not iana.isEmpty() else ""
        return windows or "UTC"

    def _event_editor(self, row=None, **kwargs) -> EventEditor:
        box = self._calendar_box(row["account_id"] if row else None)
        me = box.display_name if box else ""

        def schedule(addresses, day, done):
            if box is None:
                return
            start = dt.datetime.combine(day, dt.time()).astimezone()
            end = start + dt.timedelta(days=1)
            utc = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            self._run(lambda: box.graph.schedule(addresses, utc(start), utc(end)), done,
                      activity="Checking availability…")

        dlg = EventEditor(self.store.calendar_rows(), self.store.category_colors(),
                          suggestions(self.store), schedule, me, row=row, parent=self,
                          tz=self._time_zone(), **kwargs)
        if row is not None and box is not None:  # the full notes (the list has a preview)
            event_id = row["id"]

            def load_body():
                content_type, content = box.graph.event_body(event_id)
                wanted = cid_refs(content) if content_type == "html" else set()
                if wanted:  # its pictures, as data: URIs the editor can show
                    try:
                        content = resolve_cids(content, box.graph.inline_images(
                            box.graph.event_path(event_id), wanted))
                    except Exception:  # (the notes without pictures, then)
                        log.exception("pictures of event %s", event_id)
                return content_type, content

            self._run(load_body, lambda body: dlg.set_body(*body))
            self._run(lambda: [Attachment.from_server(a) for a in box.graph.attachments(
                box.graph.event_path(event_id)) if not a.get("isInline")],
                dlg.set_attachments)
        return dlg

    def new_event(self, start=None, end=None, all_day: bool = False):
        if not self._calendar_boxes():
            self.statusBar().showMessage("Sign in first", 4000)
            return
        dlg = self._event_editor(start=start, end=end, all_day=all_day)
        if dlg.exec():
            self._save_event(dlg, None)

    def _series_choice(self, row, verb: str) -> str | None:
        """For an occurrence of a repeating event: "one", "series", or None (cancelled)."""
        if not row["series_id"]:
            return "one"
        box = QMessageBox(QMessageBox.Icon.Question, "Repeating event",
                          f"\"{row['subject']}\" is part of a series. {verb.capitalize()} just "
                          "this one, or the whole series?", parent=self)
        one = box.addButton("Just this one", QMessageBox.ButtonRole.AcceptRole)
        series = box.addButton("The whole series", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return {one: "one", series: "series"}.get(box.clickedButton())

    def edit_event(self, event_id: str):
        row = self.store.event_row(event_id)
        if row is None:
            return
        choice = self._series_choice(row, "open")
        if choice == "one":
            dlg = self._event_editor(row=row)
            if dlg.exec():
                self._save_event(dlg, row)
        elif choice == "series":
            self._edit_series(row)

    def _edit_series(self, occurrence):
        """Fetch the series' master event (it has the pattern) and edit that."""
        box = self._calendar_box(occurrence["account_id"])
        if box is None:
            return
        series_id, calendar_id = occurrence["series_id"], occurrence["calendar_id"]

        def done(master: dict):
            row = calendar_event_row(master, box.account_id, calendar_id)
            dlg = self._event_editor(row=row, recurrence=master.get("recurrence"), series=True)
            if dlg.exec():
                self._save_event(dlg, row)

        self._run(lambda: box.graph.get_event(series_id), done, activity="Opening series…",
                  on_failed=lambda msg: QMessageBox.warning(
                      self, "Calendar", f"Couldn't open the series.\n\n{msg}"))

    def _open_event(self, event_id: str):
        """From a reminder: show the event in the Calendar and open it."""
        row = self.store.event_row(event_id)
        if row is None:
            return
        self.tray.show_window()
        self.show_page("calendar")
        self.calendar_page.go_to(event_span(row)[0].date())
        self.calendar_page.select_event(event_id)
        self.edit_event(event_id)

    def dismiss_reminders(self, keys: list):
        for event_id, start in keys:
            self.store.set_reminder_state(event_id, start, dismissed=True)
        self.reminders.check()
        self._reminder_calls([(eid, None) for eid, _ in keys])

    def snooze_reminders(self, keys: list, minutes: int):
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for event_id, start in keys:
            self.store.set_reminder_state(event_id, start, snoozed_until=until)
        self.reminders.check()
        self._reminder_calls([(eid, until) for eid, _ in keys])

    def _reminder_calls(self, items: list):
        """Tell Exchange too (dismiss, or snooze until), so Outlook elsewhere agrees. Best
        effort: it is already remembered here."""
        work = []
        for event_id, until in items:
            row = self.store.event_row(event_id)
            box = self._calendar_box(row["account_id"] if row else None)
            if box is not None:
                work.append((box, event_id, until))

        def job():
            for box, event_id, until in work:
                try:
                    if until:
                        box.graph.snooze_reminder(event_id, until)
                    else:
                        box.graph.dismiss_reminder(event_id)
                except Exception as exc:
                    log.info("reminder not synced for %s: %s", event_id, exc)

        if work:
            self._run(job, lambda _: None)

    def _save_event(self, dlg: EventEditor, row):
        draft, calendar_id = dlg.draft(), (row["calendar_id"] if row else dlg.calendar_id())
        cal = self.store.calendar_row(calendar_id)
        box = self._calendar_box(cal["account_id"] if cal else None)
        if box is None:
            return
        fields = draft.fields()
        meeting = bool(draft.required or draft.optional)
        # A series (new, changed, or no longer repeating): its occurrences have to be fetched
        # again. A single event or occurrence comes back whole and is stored as it is.
        series = bool(draft.recurrence) or dlg.series

        files = dlg.attachments.items()
        known = dlg.attachments_known
        attachments_changed = dlg.attachments_changed
        # pictures in the notes: inline attachments (also to remove ones taken out)
        pictures = draft.body is not None and bool(draft.inline or dlg.had_pictures)
        extra = attachments_changed or pictures

        def upload(event_id: str):
            path = box.graph.event_path(event_id)
            if attachments_changed:  # add the new files, remove the ones taken out
                reconcile(box.graph, path, files, remove_others=known)
            if pictures:
                ours = {i.content_id.lower() for i in draft.inline}
                reconcile_inline(box.graph, path, list(draft.inline),
                                 keep_others=bool(cid_refs(draft.body) - ours))

        def job():
            # Attachments and pictures go on before attendees hear about it, so the one
            # invitation or update they get has them: an existing event gets them first, a
            # new meeting is created without attendees, given them, then invited.
            if row is not None:
                if extra:
                    upload(row["id"])
                item = box.graph.save_event(fields, row["id"])
            elif meeting and extra:
                item = box.graph.save_event({k: v for k, v in fields.items()
                                             if k != "attendees"}, None, calendar_id)
                upload(item["id"])
                item = box.graph.save_event({"attendees": fields["attendees"]}, item["id"])
            else:
                item = box.graph.save_event(fields, None, calendar_id)
                if extra:
                    upload(item["id"])
            if not series:
                self.store.upsert_event(calendar_event_row(item, box.account_id, calendar_id))
            return item["id"]

        def done(event_id: str):
            self.calendar_page.selected = None if series else event_id
            self.calendar_page.reload()
            if series:
                self.refresh_calendar()
            what = "Meeting sent" if meeting and row is None else (
                "Update sent" if meeting else "Event saved")
            self.statusBar().showMessage(what, 4000)

        self._run(job, done, activity="Saving event…", on_failed=lambda msg: QMessageBox.warning(
            self, "Calendar", f"Couldn't save the event.\n\n{msg}"))

    def delete_event(self, event_id: str):
        row = self.store.event_row(event_id)
        box = self._calendar_box(row["account_id"] if row else None)
        if row is None or box is None:
            return
        choice = self._series_choice(row, "delete")
        if choice is None:
            return
        target = row["series_id"] if choice == "series" else event_id
        what = "the whole series " if choice == "series" else ""
        attendees = json.loads(row["attendees"] or "[]")
        if choice == "series":
            q = (f"Delete {what}\"{row['subject']}\"?"
                 + (f"\n\nThe {len(attendees)} attendee(s) are sent a cancellation."
                    if attendees and row["is_organizer"] else ""))
        elif attendees and row["is_organizer"]:
            q = (f"Cancel \"{row['subject']}\"?\n\nThe {len(attendees)} attendee(s) are sent "
                 "a cancellation.")
        elif attendees:
            q = (f"Delete \"{row['subject']}\" from your calendar?\n\nThe organizer isn't "
                 "told; to let them know, use Decline instead.")
        else:
            q = f"Delete \"{row['subject']}\"?"
        if QMessageBox.question(self, "Calendar", q) != QMessageBox.StandardButton.Yes:
            return

        def job():
            box.graph.delete_event(target)
            self.store.delete_event(event_id)

        def done(_):
            self.calendar_page.reload()
            self.statusBar().showMessage("Deleted", 3000)
            if choice == "series":
                self.refresh_calendar()  # the other occurrences go too

        self._run(job, done, activity="Deleting event…", on_failed=lambda msg: QMessageBox.warning(
            self, "Calendar", f"Couldn't delete the event.\n\n{msg}"))

    def respond_event(self, event_id: str, response: str):
        row = self.store.event_row(event_id)
        box = self._calendar_box(row["account_id"] if row else None)
        if row is None or box is None:
            return
        words = {"accept": "Accepted", "tentativelyAccept": "Tentatively accepted",
                 "decline": "Declined"}

        def job():
            box.graph.respond_event(event_id, response)

        def done(_):
            self.statusBar().showMessage(f"{words[response]}: {row['subject']}", 4000)
            self.refresh_calendar()  # the server may move a declined meeting out

        self._run(job, done, activity="Sending response…",
                  on_failed=lambda msg: QMessageBox.warning(
                      self, "Calendar", f"Couldn't send the response.\n\n{msg}"))

    # -- preferences ----------------------------------------------------
    def show_preferences(self):
        dlg = PreferencesDialog(self.settings, self._clear_cache, self, store=self.store,
                                accounts=self._compose_accounts())
        if dlg.exec():
            self._apply_settings()
            self._no_older.clear()
            self._refresh_messages()
            self.statusBar().showMessage("Preferences saved", 3000)
            self.sync_current()  # a longer sync period fetches the extra mail now

    def _apply_settings(self):
        s = self.settings
        self.mailboxes.configure(s.download_days() or 36500,
                                 s.get("cache/max_mb") * 1024 * 1024)
        self.timer.setInterval(s.get("sync/interval_min") * 60_000)
        self.tray.setVisible(s.get("tray/show") and QSystemTrayIcon.isSystemTrayAvailable())
        # With close-to-tray, closing the window mustn't end the program.
        QApplication.instance().setQuitOnLastWindowClosed(not self.close_to_tray())

    def _list_cutoff(self) -> str | None:
        days = self.settings.get("list/window_days")
        if not days:
            return None
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _clear_cache(self):
        self.store.clear_bodies()
        self.statusBar().showMessage("Cached message bodies cleared", 3000)
