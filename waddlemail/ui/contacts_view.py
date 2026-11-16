"""The Contacts view: sources | searchable list | details card.

Sources: all contacts, each personal contact folder, contact groups, the organization
directory, and recent recipients (people you send to and reply to). With several accounts,
"Contacts from" above them picks whose (or all accounts'): each account's contacts, groups,
directory (its organization) and recipients are its own. New contacts and groups go to it.

- Personal contacts: new, edit (with photo, addresses, birthday, ...), delete.
- Contact groups: new, edit (name, notes, members), delete; "Add to group" adds whoever is
  selected in any list.
- Directory people and recent recipients can be copied into your contacts.
- The details card shows the person's photo (fetched once when first shown, then cached), and
  any address in it starts a new message; a group can be emailed whole.

The view only reads the store. Changes, refreshes and photo downloads are asked for through
signals and done by MainWindow in the background.
"""
from __future__ import annotations

import html
import json
import time

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPalette, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QLabel, QLineEdit, QMenu, QSplitter, QTextBrowser, QToolBar,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..contacts import DIRECTORY, PERSONAL, format_address, format_postal, member_payload
from ..store import Store
from . import iconset
from .contact_dialogs import ContactDialog, GroupDialog, avatar

KEY_ROLE = Qt.ItemDataRole.UserRole       # (kind, id) of a list row
SOURCE_ROLE = Qt.ItemDataRole.UserRole + 1  # (kind, folder) of a source row

ALL, FOLDER, GROUPS, DIR, RECENT = "all", "folder", "groups", "directory", "recent"
PHOTO_RETRY = 7 * 86400  # ask again for a photo someone didn't have, after a week


class ContactsView(QWidget):
    composeRequested = Signal(list)                       # addresses ("Name <address>")
    refreshRequested = Signal()
    saveRequested = Signal(dict, str, str, str, object)   # fields, id ("" new), folder, name, photo
    deleteRequested = Signal(str)                         # contact id
    groupSaveRequested = Signal(str, str, str, list, list)  # id ("" new), name, notes, add, remove
    groupDeleteRequested = Signal(str)
    photoWanted = Signal(str, str)                        # contact id, folder id

    def __init__(self, store: Store, suggestions=lambda: [], parent=None):
        """`suggestions()` gives contacts.suggestions() for the group editor's completion."""
        super().__init__(parent)
        self.store = store
        self._suggestions = suggestions
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QToolBar("Contacts")
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setIconSize(QSize(16, 16))
        self.act_new = bar.addAction(iconset.load("user-plus"), "New contact", self.new_item)
        self.act_edit = bar.addAction(iconset.load("pencil"), "Edit", self.edit_item)
        self.act_delete = bar.addAction(iconset.load("trash-2"), "Delete", self.delete_item)
        bar.addSeparator()
        self.act_add = bar.addAction(iconset.load("user-plus"), "Add to my contacts",
                                     self.copy_to_contacts)
        self.group_button = QToolButton()  # "Add to group ▾": the groups, chosen per click
        self.group_button.setText("Add to group")
        self.group_button.setIcon(iconset.load("users-round"))
        self.group_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.group_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._group_menu = QMenu(self)
        self._group_menu.aboutToShow.connect(self._fill_group_menu)
        self.group_button.setMenu(self._group_menu)
        bar.addWidget(self.group_button)
        bar.addSeparator()
        self.act_mail = bar.addAction(iconset.load("mail-plus"), "New email", self.email_selected)
        bar.addSeparator()
        self.act_refresh = bar.addAction(iconset.load("refresh-cw"), "Refresh",
                                         self.refreshRequested.emit)
        self.act_refresh.setToolTip("Download contacts, contact groups and the directory again")
        lay.addWidget(bar)
        self.banner = QLabel()  # a source that couldn't be loaded
        self.banner.setWordWrap(True)
        self.banner.setContentsMargins(10, 6, 10, 6)
        self.banner.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.sources = QTreeWidget()
        self.sources.setHeaderHidden(True)
        self.sources.setRootIsDecorated(False)
        self.sources.setIndentation(14)
        self.sources.currentItemChanged.connect(lambda *_: self._fill_list())
        self.accounts = QComboBox()  # "Contacts from": one account's, or everyone's
        self.accounts.setToolTip("Whose contacts, groups, directory and recipients to show")
        self.accounts.activated.connect(self._account_chosen)
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(2)
        self.accounts_label = QLabel("Contacts from:")
        self.accounts_label.setContentsMargins(6, 4, 6, 0)
        llay.addWidget(self.accounts_label)
        llay.addWidget(self.accounts)
        llay.addWidget(self.sources, 1)

        middle = QWidget()
        mlay = QVBoxLayout(middle)
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.setSpacing(0)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Search contacts")
        self.search.addAction(iconset.load("search"), QLineEdit.ActionPosition.LeadingPosition)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._fill_list)
        self.search.textChanged.connect(lambda _: self._debounce.start())
        self.list = QTreeWidget()
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setSortingEnabled(True)
        self.list.itemSelectionChanged.connect(self._show_details)
        self.list.itemDoubleClicked.connect(lambda *_: self._double_clicked())
        mlay.addWidget(self.search)
        mlay.addWidget(self.list, 1)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(self._link)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(middle)
        split.addWidget(self.details)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 460, 380])
        lay.addWidget(split, 1)
        self.reload()

    # -- whose contacts -------------------------------------------------------------
    def account(self) -> str | None:
        """The account shown (None: all of them)."""
        return self.accounts.currentData() or None

    def _fill_accounts(self) -> None:
        """One entry per account (the account's own mailbox name), then All accounts; the
        choice is remembered. Hidden with just one account."""
        own = [(m["account_id"], m["display_name"]) for m in self.store.mailbox_rows()
               if not m["address"]]
        keep = self.accounts.currentData() if self.accounts.count() else \
            self.store.get_state("contacts:account")
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for account_id, name in own:
            self.accounts.addItem(iconset.load("user-round"), name, account_id)
        self.accounts.addItem(iconset.load("users-round"), "All accounts", "")
        index = self.accounts.findData(keep) if keep is not None else -1
        self.accounts.setCurrentIndex(max(index, 0))
        self.accounts.blockSignals(False)
        for w in (self.accounts, self.accounts_label):
            w.setVisible(len(own) > 1)

    def _account_chosen(self, _index: int) -> None:
        self.store.set_state("contacts:account", self.accounts.currentData() or "")
        self.reload()

    # -- sources ----------------------------------------------------------------
    def reload(self) -> None:
        """Re-read everything from the store, keeping the chosen source and selection."""
        self._fill_accounts()
        current = self.sources.currentItem()
        keep = current.data(0, SOURCE_ROLE) if current else (ALL, None)
        selected = self.selected_keys()
        self.sources.blockSignals(True)
        self.sources.clear()

        def add(text, icon, source, parent=None):
            item = QTreeWidgetItem([text])
            item.setIcon(0, iconset.load(icon))
            item.setData(0, SOURCE_ROLE, source)
            (parent.addChild(item) if parent else self.sources.addTopLevelItem(item))
            return item

        everyone = add("All contacts", "contact", (ALL, None))
        for f in self.store.contact_folders(self.account()):
            add(f["folder_name"] or "Contacts", "book-user", (FOLDER, f["folder_id"]), everyone)
        add("Contact groups", "users-round", (GROUPS, None))
        add("Organization", "users-round", (DIR, None))
        add("Recent recipients", "reply", (RECENT, None))
        self.sources.expandAll()
        self.sources.blockSignals(False)
        for i in range(self.sources.topLevelItemCount()):
            top = self.sources.topLevelItem(i)
            for item in [top] + [top.child(j) for j in range(top.childCount())]:
                if item.data(0, SOURCE_ROLE) == keep:
                    self.sources.setCurrentItem(item)
        if self.sources.currentItem() is None:
            self.sources.setCurrentItem(everyone)
        self._fill_list(select=selected)

    def source(self) -> tuple[str, str | None]:
        item = self.sources.currentItem()
        return item.data(0, SOURCE_ROLE) if item else (ALL, None)

    def show_source(self, kind: str) -> None:
        for i in range(self.sources.topLevelItemCount()):
            if self.sources.topLevelItem(i).data(0, SOURCE_ROLE)[0] == kind:
                self.sources.setCurrentItem(self.sources.topLevelItem(i))

    def show_problems(self, problems: list[str]) -> None:
        self.banner.setText("\n".join(problems))
        self.banner.setVisible(bool(problems))

    # -- the list ---------------------------------------------------------------
    def _columns(self, labels: list[str]):
        self.list.setColumnCount(len(labels))  # (setHeaderLabels alone keeps extra columns)
        self.list.setHeaderLabels(labels)

    def _fill_list(self, select: list | None = None) -> None:
        kind, folder = self.source()
        self.act_new.setText("New group" if kind == GROUPS else "New contact")
        search = self.search.text().strip()
        self.list.setSortingEnabled(False)
        self.list.clear()
        if kind == GROUPS:
            self._columns(["Group", "Members"])
            for g in self.store.groups(search, self.account()):
                n = len(json.loads(g["members"] or "[]"))
                self._row([g["name"], str(n)], ("group", g["id"]), "users-round")
        elif kind == RECENT:
            self._columns(["Name", "Email", "Sent", "Replied", "Last"])
            for r in self.store.recent_recipients(search, account_id=self.account()):
                self._row([r["name"] or r["address"], r["address"], str(r["sent"]),
                           str(r["replied"]), (r["last_used"] or "")[:10]],
                          ("recent", r["address"]), "reply")
        else:
            self._columns(["Name", "Email", "Company", "Title"])
            source = DIRECTORY if kind == DIR else PERSONAL
            rows = self.store.contacts(source, folder if kind == FOLDER else None, search,
                                       self.account())
            for c in rows:
                emails = json.loads(c["emails"] or "[]")
                self._row([c["display_name"], emails[0][1] if emails else "",
                           c["company"] or "", c["job_title"] or ""], ("contact", c["id"]),
                          "users-round" if source == DIRECTORY else "contact")
        self.list.setSortingEnabled(kind != RECENT)  # recent: most used first
        if kind != RECENT:
            self.list.sortItems(0, Qt.SortOrder.AscendingOrder)
        for col, width in enumerate((220, 230, 160)):
            self.list.setColumnWidth(col, width)
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if select and item.data(0, KEY_ROLE) in select:
                item.setSelected(True)
        if not self.list.selectedItems() and self.list.topLevelItemCount():
            self._select(self.list.topLevelItem(0))
        self._show_details()

    def _select(self, item: QTreeWidgetItem):
        # (in a multi-selection list, setCurrentItem alone moves the cursor but doesn't select)
        self.list.clearSelection()
        self.list.setCurrentItem(item)
        item.setSelected(True)
        self.list.scrollToItem(item)

    def _row(self, texts, key, icon):
        item = QTreeWidgetItem(texts)
        item.setIcon(0, iconset.load(icon))
        item.setData(0, KEY_ROLE, key)
        self.list.addTopLevelItem(item)

    def selected_keys(self) -> list[tuple[str, str]]:
        return [i.data(0, KEY_ROLE) for i in self.list.selectedItems()]

    def select_key(self, key: tuple[str, str]):
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if item.data(0, KEY_ROLE) == key:
                self._select(item)
                return

    def select_contact(self, contact_id: str):
        self.select_key(("contact", contact_id))

    # -- details card -----------------------------------------------------------------
    def _is_personal(self, key) -> bool:
        if key[0] != "contact":
            return False
        row = self.store.contact_row(key[1])
        return row is not None and row["source"] == PERSONAL

    def _show_details(self) -> None:
        keys = self.selected_keys()
        one = keys[0] if len(keys) == 1 else None
        editable = one is not None and (one[0] == "group" or self._is_personal(one))
        self.act_edit.setEnabled(editable)
        self.act_delete.setEnabled(editable)
        self.act_add.setEnabled(one is not None and one[0] in ("contact", "recent")
                                and not self._is_personal(one))
        self.group_button.setEnabled(any(k[0] != "group" for k in keys))
        self.act_mail.setEnabled(bool(keys))
        if one is None:
            self.details.setHtml(f"<p style='color:gray'>{len(keys)} selected</p>" if keys
                                 else "<p style='color:gray'>Nothing to show here yet.</p>")
            return
        kind, key = one
        if kind == "group":
            self._group_card(key)
        elif kind == "recent":
            r = self._recent(key)
            if r:
                self._card(r["name"] or r["address"], [], [[r["name"], r["address"]]], [],
                           [f"You sent them {r['sent']} message(s) and replied to "
                            f"{r['replied']} of theirs; last on {(r['last_used'] or '')[:10]}."])
        else:
            c = self.store.contact_row(key)
            if c:
                self._contact_card(c)

    def _recent(self, address: str):
        return next((r for r in self.store.recent_recipients(address)
                     if r["address"] == address), None)

    def _photo_for(self, c) -> bytes | None:
        """The cached photo, asking for it in the background if it isn't known (yet)."""
        cached = self.store.photo(c["id"])
        if cached is None or (cached[0] is None and time.time() - cached[1] > PHOTO_RETRY):
            self.photoWanted.emit(c["id"], c["folder_id"] or "")
        return cached[0] if cached else None

    def photo_arrived(self, contact_id: str):
        """MainWindow fetched a photo: redraw if that person is still shown."""
        if self.selected_keys() == [("contact", contact_id)]:
            self._show_details()

    def _contact_card(self, c):
        sub = [x for x in (c["job_title"], c["department"], c["company"]) if x]
        lines = []
        if c["nickname"]:
            lines.append(f"Nickname: {c['nickname']}")
        if c["office"]:
            lines.append(f"Office: {c['office']}")
        if c["birthday"]:
            lines.append(f"Birthday: {c['birthday']}")
        addresses = [(label, format_postal(parts))
                     for label, parts in json.loads(c["addresses"] or "[]")]
        where = ("Organization directory" if c["source"] == DIRECTORY
                 else f"In your contacts ({c['folder_name'] or 'Contacts'})")
        self._card(c["display_name"], sub, json.loads(c["emails"] or "[]"),
                   json.loads(c["phones"] or "[]"), lines, where, self._photo_for(c),
                   addresses, c["website"], c["notes"])

    def _card(self, name, subtitle, emails, phones, lines, where="", photo=None,
              addresses=(), website=None, notes=None):
        doc = self.details.document()
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl("avatar://me"),
                        avatar(name or "?", 64, photo))
        e = html.escape
        parts = [f"<table cellpadding='4'><tr><td><img src='avatar://me' width='64' height='64'>"
                 f"</td><td valign='middle'><span style='font-size:large;font-weight:bold'>"
                 f"{e(name)}</span><br><span style='color:gray'>{e(' · '.join(subtitle))}"
                 f"</span></td></tr></table>"]
        if emails:
            parts.append("<p><b>Email</b><br>" + "<br>".join(
                f"<a href='mailto:{e(a)}'>{e(a)}</a>" for _, a in emails) + "</p>")
        if phones:
            parts.append("<p><b>Phone</b><br>" + "<br>".join(
                f"{e(label)}: <a href='tel:{e(n)}'>{e(n)}</a>" for label, n in phones) + "</p>")
        for label, text in addresses:
            parts.append(f"<p><b>{e(label)} address</b><br>"
                         + e(text).replace("\n", "<br>") + "</p>")
        if website:
            url = website if "://" in website else "https://" + website
            parts.append(f"<p><b>Website</b><br><a href='{e(url)}'>{e(website)}</a></p>")
        for line in lines:
            parts.append(f"<p>{e(line)}</p>")
        if notes:
            parts.append("<p><b>Notes</b><br>" + e(notes).replace("\n", "<br>") + "</p>")
        if where:
            parts.append(f"<p style='color:gray'>{e(where)}</p>")
        self.details.setHtml("".join(parts))

    def _group_card(self, group_id):
        g = self.store.group_row(group_id)
        if g is None:
            return
        members = json.loads(g["members"] or "[]")
        e = html.escape
        rows = "".join(
            f"<tr><td>{e(m[0])}</td><td>"
            + (f"<a href='mailto:{e(m[1])}'>{e(m[1])}</a>" if m[1] else
               "<span style='color:gray'>(no address)</span>") + "</td></tr>"
            for m in members)
        self.details.setHtml(
            f"<p><span style='font-size:large;font-weight:bold'>{e(g['name'])}</span><br>"
            f"<span style='color:gray'>Contact group · {len(members)} member(s)</span></p>"
            f"<p><a href='group:{e(group_id)}'>Email everyone in this group</a></p>"
            f"<table cellpadding='3'>{rows}</table>"
            + (f"<p>{e(g['notes'])}</p>" if g["notes"] else ""))

    def _link(self, url: QUrl):
        if url.scheme() == "mailto":
            self.composeRequested.emit([url.path()])
        elif url.scheme() == "group":
            self.composeRequested.emit(self._group_addresses(url.path()))
        elif url.scheme() in ("http", "https", "tel"):
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    # -- addresses ----------------------------------------------------------------------
    def _group_addresses(self, group_id: str) -> list[str]:
        g = self.store.group_row(group_id)
        return [format_address(m[0], m[1]) for m in json.loads(g["members"] or "[]") if m[1]] \
            if g else []

    def _people(self, keys) -> list[tuple[str, str, str]]:
        """(name, address, source) for selected people (groups left out)."""
        out = []
        for kind, key in keys:
            if kind == "recent":
                r = self._recent(key)
                out.append(((r["name"] if r else "") or key, key, "recent"))
            elif kind == "contact":
                c = self.store.contact_row(key)
                emails = json.loads(c["emails"] or "[]") if c else []
                if emails:
                    out.append((c["display_name"], emails[0][1], c["source"]))
        return out

    def addresses_for(self, keys) -> list[str]:
        out = []
        for kind, key in keys:
            if kind == "group":
                out += self._group_addresses(key)
        out += [format_address(n, a) for n, a, _ in self._people(keys)]
        return out

    # -- actions ------------------------------------------------------------------------
    def email_selected(self):
        addresses = self.addresses_for(self.selected_keys())
        if addresses:
            self.composeRequested.emit(addresses)

    def _double_clicked(self):
        if self.act_edit.isEnabled():
            self.edit_item()
        else:
            self.email_selected()

    def _folders(self) -> list[tuple[str, str]]:
        folders = [(f["folder_id"], f["folder_name"] or "Contacts")
                   for f in self.store.contact_folders(self.account())]
        return folders or [("", "Contacts")]

    def new_item(self):
        if self.source()[0] == GROUPS:
            self.new_group()
        else:
            self.new_contact()

    def edit_item(self):
        keys = self.selected_keys()
        if len(keys) != 1:
            return
        if keys[0][0] == "group":
            self.edit_group(keys[0][1])
        elif self._is_personal(keys[0]):
            self.edit_contact(keys[0][1])

    def delete_item(self):
        keys = self.selected_keys()
        if len(keys) != 1:
            return
        if keys[0][0] == "group":
            self.groupDeleteRequested.emit(keys[0][1])
        elif self._is_personal(keys[0]):
            self.deleteRequested.emit(keys[0][1])

    def new_contact(self, prefill: dict | None = None):
        dlg = ContactDialog(self._folders(), prefill=prefill, parent=self)
        kind, folder = self.source()
        if kind == FOLDER:
            dlg.folder.setCurrentIndex(max(dlg.folder.findData(folder), 0))
        if dlg.exec():
            self.saveRequested.emit(dlg.values(), "", dlg.folder_id(), dlg.folder_name(),
                                    dlg.photo_bytes)

    def edit_contact(self, contact_id: str):
        row = self.store.contact_row(contact_id)
        if row is None or row["source"] != PERSONAL:
            return
        cached = self.store.photo(contact_id)
        dlg = ContactDialog(self._folders(), row=row, photo=cached[0] if cached else None,
                            parent=self)
        if dlg.exec():
            self.saveRequested.emit(dlg.values(), row["id"], row["folder_id"] or "",
                                    row["folder_name"] or "Contacts", dlg.photo_bytes)

    def copy_to_contacts(self):
        keys = self.selected_keys()
        if len(keys) != 1:
            return
        kind, key = keys[0]
        if kind == "recent":
            r = self._recent(key)
            prefill = {"name": (r["name"] if r else "") or "", "address": key}
        else:
            c = self.store.contact_row(key)
            emails = json.loads(c["emails"] or "[]") if c else []
            prefill = {"name": c["display_name"], "address": emails[0][1] if emails else "",
                       "company": c["company"], "job_title": c["job_title"],
                       "department": c["department"]} if c else {}
        self.new_contact(prefill)

    def new_group(self):
        dlg = GroupDialog(self._suggestions(), parent=self)
        if dlg.exec():
            added, _ = dlg.changes()
            self.groupSaveRequested.emit("", dlg.name.text().strip(),
                                         dlg.notes.toPlainText().strip(),
                                         [member_payload(m[0], m[1], m[2]) for m in added], [])

    def edit_group(self, group_id: str):
        g = self.store.group_row(group_id)
        if g is None:
            return
        dlg = GroupDialog(self._suggestions(), group=g, parent=self)
        if dlg.exec():
            added, removed = dlg.changes()
            self.groupSaveRequested.emit(group_id, dlg.name.text().strip(),
                                         dlg.notes.toPlainText().strip(),
                                         [member_payload(m[0], m[1], m[2]) for m in added],
                                         removed)

    def _fill_group_menu(self):
        self._group_menu.clear()
        people = self._people(self.selected_keys())
        groups = self.store.groups(account_id=self.account())
        for g in groups:
            act = self._group_menu.addAction(iconset.load("users-round"), g["name"])
            act.triggered.connect(lambda _=False, gid=g["id"]: self.add_to_group(gid, people))
        if groups:
            self._group_menu.addSeparator()
        self._group_menu.addAction(iconset.load("user-plus"), "New group with these…",
                                   lambda: self._new_group_with(people))

    def add_to_group(self, group_id: str, people: list[tuple[str, str, str]]):
        g = self.store.group_row(group_id)
        if g is None or not people:
            return
        present = {(m[1] or "").lower() for m in json.loads(g["members"] or "[]")}
        added = [member_payload(n, a, s) for n, a, s in people if a.lower() not in present]
        if added:
            self.groupSaveRequested.emit(group_id, g["name"], g["notes"] or "", added, [])

    def _new_group_with(self, people):
        dlg = GroupDialog(self._suggestions(), parent=self)
        for n, a, s in people:
            dlg._add_item([n, a, s, "", ""])
        if dlg.exec():
            self.groupSaveRequested.emit("", dlg.name.text().strip(),
                                         dlg.notes.toPlainText().strip(),
                                         [member_payload(m[0], m[1], m[2])
                                          for m in dlg.current_members()], [])
