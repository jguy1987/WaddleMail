"""The Contacts view: sources | searchable list | details card.

Sources: all contacts, each personal contact folder, contact groups, the organization
directory, and recent recipients (people you send to and reply to). Personal contacts can be
created, edited and deleted; directory people and recent recipients can be copied into your
contacts. Any address in the details card starts a new message; a group can be emailed whole.

The view only reads the store. Changes and refreshes are asked for through signals and done
by MainWindow in the background.
"""
from __future__ import annotations

import html
import json

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QSplitter, QTextBrowser, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..contacts import DIRECTORY, PERSONAL, format_address
from ..store import Store
from . import iconset

KEY_ROLE = Qt.ItemDataRole.UserRole       # (kind, id) of a list row
SOURCE_ROLE = Qt.ItemDataRole.UserRole + 1  # (kind, folder) of a source row

ALL, FOLDER, GROUPS, DIR, RECENT = "all", "folder", "groups", "directory", "recent"
_AVATAR_COLORS = ["#4f8cff", "#2f9e44", "#e8590c", "#ae3ec9", "#1098ad", "#d6336c", "#5c7cfa"]


def initials(name: str) -> str:
    parts = [p for p in name.replace(",", " ").split() if p[:1].isalnum()]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def avatar(name: str, size: int = 64) -> QImage:
    """A coloured circle with the person's initials (the colour follows the name)."""
    img = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32)
    img.setDevicePixelRatio(2)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(_AVATAR_COLORS[sum(map(ord, name)) % len(_AVATAR_COLORS)]))
    p.drawEllipse(QRectF(0, 0, size, size))
    font = QFont()
    font.setPixelSize(int(size * 0.38))
    font.setBold(True)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, initials(name))
    p.end()
    return img


class ContactDialog(QDialog):
    """New / edit a personal contact. values() gives Graph contact fields."""

    def __init__(self, folders: list[tuple[str, str]], row=None, prefill: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit contact" if row else "New contact")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.given, self.surname, self.display = QLineEdit(), QLineEdit(), QLineEdit()
        self.company, self.title, self.department = QLineEdit(), QLineEdit(), QLineEdit()
        self.emails = [QLineEdit() for _ in range(3)]
        self.work, self.mobile, self.home = QLineEdit(), QLineEdit(), QLineEdit()
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(80)
        self.folder = QComboBox()
        for folder_id, name in folders:
            self.folder.addItem(name, folder_id)
        form.addRow("First name:", self.given)
        form.addRow("Last name:", self.surname)
        form.addRow("Display as:", self.display)
        self.display.setPlaceholderText("First and last name")
        for i, edit in enumerate(self.emails):
            form.addRow("Email:" if i == 0 else "", edit)
        form.addRow("Company:", self.company)
        form.addRow("Job title:", self.title)
        form.addRow("Department:", self.department)
        form.addRow("Work phone:", self.work)
        form.addRow("Mobile:", self.mobile)
        form.addRow("Home phone:", self.home)
        form.addRow("Notes:", self.notes)
        if row is None and len(folders) > 1:
            form.addRow("Save in:", self.folder)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        form.addRow(box)
        if row is not None:
            self._load(row)
        elif prefill:
            self.display.setText(prefill.get("name", ""))
            parts = prefill.get("name", "").split()
            if len(parts) >= 2:
                self.given.setText(" ".join(parts[:-1]))
                self.surname.setText(parts[-1])
            self.emails[0].setText(prefill.get("address", ""))
            self.company.setText(prefill.get("company") or "")
            self.title.setText(prefill.get("job_title") or "")
            self.department.setText(prefill.get("department") or "")
        self.given.setFocus()

    def _load(self, row):
        self.given.setText(row["given_name"] or "")
        self.surname.setText(row["surname"] or "")
        self.display.setText(row["display_name"] or "")
        for edit, (_, address) in zip(self.emails, json.loads(row["emails"] or "[]")):
            edit.setText(address)
        self.company.setText(row["company"] or "")
        self.title.setText(row["job_title"] or "")
        self.department.setText(row["department"] or "")
        phones = json.loads(row["phones"] or "[]")
        for label, edit in (("Work", self.work), ("Mobile", self.mobile), ("Home", self.home)):
            edit.setText(next((n for k, n in phones if k == label), ""))
        self.notes.setPlainText(row["notes"] or "")

    def _accept(self):
        if any(e.text().strip() for e in (self.given, self.surname, self.display,
                                           self.company, *self.emails)):
            self.accept()

    def folder_id(self) -> str:
        return self.folder.currentData() or ""

    def folder_name(self) -> str:
        return self.folder.currentText() or "Contacts"

    def values(self) -> dict:
        given, surname = self.given.text().strip(), self.surname.text().strip()
        display = self.display.text().strip() or " ".join(x for x in (given, surname) if x)
        emails = [e.text().strip() for e in self.emails if e.text().strip()]
        return {
            "givenName": given, "surname": surname,
            "displayName": display or (emails[0] if emails else ""),
            "emailAddresses": [{"address": a, "name": display or a} for a in emails],
            "companyName": self.company.text().strip(),
            "jobTitle": self.title.text().strip(),
            "department": self.department.text().strip(),
            "businessPhones": [self.work.text().strip()] if self.work.text().strip() else [],
            "mobilePhone": self.mobile.text().strip(),
            "homePhones": [self.home.text().strip()] if self.home.text().strip() else [],
            "personalNotes": self.notes.toPlainText().strip(),
        }


class ContactsView(QWidget):
    composeRequested = Signal(list)              # addresses ("Name <address>")
    refreshRequested = Signal()
    saveRequested = Signal(dict, str, str, str)  # fields, contact id ("" = new), folder, name
    deleteRequested = Signal(str)                # contact id

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QToolBar("Contacts")
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setIconSize(QSize(16, 16))
        self.act_new = bar.addAction(iconset.load("user-plus"), "New contact", self.new_contact)
        self.act_edit = bar.addAction(iconset.load("pencil"), "Edit", self.edit_contact)
        self.act_add = bar.addAction(iconset.load("user-plus"), "Add to my contacts",
                                     self.copy_to_contacts)
        self.act_delete = bar.addAction(iconset.load("trash-2"), "Delete", self.delete_contact)
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

        middle = QWidget()
        mlay = QVBoxLayout(middle)
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.setSpacing(0)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Search contacts")
        self.search.addAction(iconset.load("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setContentsMargins(6, 4, 6, 4)
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
        split.addWidget(self.sources)
        split.addWidget(middle)
        split.addWidget(self.details)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 460, 380])
        lay.addWidget(split, 1)
        self.reload()

    # -- sources ----------------------------------------------------------------
    def reload(self) -> None:
        """Re-read everything from the store, keeping the chosen source and selection."""
        current = self.sources.currentItem()
        keep = current.data(0, SOURCE_ROLE) if current else (ALL, None)
        selected = [i.data(0, KEY_ROLE) for i in self.list.selectedItems()]
        self.sources.blockSignals(True)
        self.sources.clear()

        def add(text, icon, source, parent=None):
            item = QTreeWidgetItem([text])
            item.setIcon(0, iconset.load(icon))
            item.setData(0, SOURCE_ROLE, source)
            (parent.addChild(item) if parent else self.sources.addTopLevelItem(item))
            return item

        everyone = add("All contacts", "contact", (ALL, None))
        for f in self.store.contact_folders():
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

    def show_problems(self, problems: list[str]) -> None:
        self.banner.setText("\n".join(problems))
        self.banner.setVisible(bool(problems))

    # -- the list ---------------------------------------------------------------
    def _fill_list(self, select: list | None = None) -> None:
        kind, folder = self.source()
        search = self.search.text().strip()
        self.list.setSortingEnabled(False)
        self.list.clear()
        if kind == GROUPS:
            self._columns(["Group", "Members"])
            for g in self.store.groups(search):
                n = len(json.loads(g["members"] or "[]"))
                self._row([g["name"], str(n)], ("group", g["id"]), "users-round")
        elif kind == RECENT:
            self._columns(["Name", "Email", "Sent", "Replied", "Last"])
            for r in self.store.recent_recipients(search):
                self._row([r["name"] or r["address"], r["address"], str(r["sent"]),
                           str(r["replied"]), (r["last_used"] or "")[:10]],
                          ("recent", r["address"]), "reply")
        else:
            self._columns(["Name", "Email", "Company", "Title"])
            source = DIRECTORY if kind == DIR else PERSONAL
            rows = self.store.contacts(source, folder if kind == FOLDER else None, search)
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

    def _columns(self, labels: list[str]):
        self.list.setColumnCount(len(labels))  # (setHeaderLabels alone keeps extra columns)
        self.list.setHeaderLabels(labels)

    def _row(self, texts, key, icon):
        item = QTreeWidgetItem(texts)
        item.setIcon(0, iconset.load(icon))
        item.setData(0, KEY_ROLE, key)
        self.list.addTopLevelItem(item)

    def selected_keys(self) -> list[tuple[str, str]]:
        return [i.data(0, KEY_ROLE) for i in self.list.selectedItems()]

    # -- details card -----------------------------------------------------------------
    def _show_details(self) -> None:
        keys = self.selected_keys()
        personal = len(keys) == 1 and keys[0][0] == "contact" and \
            (self.store.contact_row(keys[0][1]) or {"source": ""})["source"] == PERSONAL
        self.act_edit.setEnabled(personal)
        self.act_delete.setEnabled(personal)
        self.act_add.setEnabled(len(keys) == 1 and not personal and keys[0][0] != "group")
        self.act_mail.setEnabled(bool(keys))
        if len(keys) != 1:
            self.details.setHtml(f"<p style='color:gray'>{len(keys)} selected</p>" if keys
                                 else "<p style='color:gray'>Nothing to show here yet.</p>")
            return
        kind, key = keys[0]
        if kind == "group":
            self._group_card(key)
        elif kind == "recent":
            r = next((r for r in self.store.recent_recipients(key) if r["address"] == key), None)
            if r:
                self._card(r["name"] or r["address"], [], [[r["name"], r["address"]]], [],
                           [f"You sent them {r['sent']} message(s) and replied to "
                            f"{r['replied']} of theirs; last on {(r['last_used'] or '')[:10]}."])
        else:
            c = self.store.contact_row(key)
            if c:
                sub = [x for x in (c["job_title"], c["department"], c["company"]) if x]
                lines = [f"Office: {c['office']}"] if c["office"] else []
                if c["notes"]:
                    lines.append(c["notes"])
                where = ("Organization directory" if c["source"] == DIRECTORY
                         else f"In your contacts ({c['folder_name'] or 'Contacts'})")
                self._card(c["display_name"], sub, json.loads(c["emails"] or "[]"),
                           json.loads(c["phones"] or "[]"), lines, where)

    def _card(self, name, subtitle, emails, phones, lines, where=""):
        doc = self.details.document()
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl("avatar://me"),
                        avatar(name or "?"))
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
        for line in lines:
            parts.append(f"<p>{e(line)}</p>")
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
            f"<tr><td>{e(n)}</td><td>"
            + (f"<a href='mailto:{e(a)}'>{e(a)}</a>" if a else
               "<span style='color:gray'>(no address)</span>") + "</td></tr>"
            for n, a, _ in members)
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

    # -- actions ------------------------------------------------------------------------
    def _group_addresses(self, group_id: str) -> list[str]:
        g = self.store.group_row(group_id)
        return [format_address(n, a) for n, a, _ in json.loads(g["members"] or "[]") if a] \
            if g else []

    def addresses_for(self, keys) -> list[str]:
        out = []
        for kind, key in keys:
            if kind == "group":
                out += self._group_addresses(key)
            elif kind == "recent":
                r = next((r for r in self.store.recent_recipients(key) if r["address"] == key),
                         None)
                out.append(format_address(r["name"] if r else "", key))
            else:
                c = self.store.contact_row(key)
                emails = json.loads(c["emails"] or "[]") if c else []
                if emails:
                    out.append(format_address(c["display_name"], emails[0][1]))
        return out

    def email_selected(self):
        addresses = self.addresses_for(self.selected_keys())
        if addresses:
            self.composeRequested.emit(addresses)

    def _double_clicked(self):
        keys = self.selected_keys()
        if len(keys) == 1 and keys[0][0] == "contact" and self.act_edit.isEnabled():
            self.edit_contact()
        else:
            self.email_selected()

    def _folders(self) -> list[tuple[str, str]]:
        folders = [(f["folder_id"], f["folder_name"] or "Contacts")
                   for f in self.store.contact_folders()]
        return folders or [("", "Contacts")]

    def new_contact(self, prefill: dict | None = None):
        dlg = ContactDialog(self._folders(), prefill=prefill, parent=self)
        kind, folder = self.source()
        if kind == FOLDER:
            dlg.folder.setCurrentIndex(max(dlg.folder.findData(folder), 0))
        if dlg.exec():
            self.saveRequested.emit(dlg.values(), "", dlg.folder_id(), dlg.folder_name())

    def edit_contact(self):
        keys = self.selected_keys()
        row = self.store.contact_row(keys[0][1]) if len(keys) == 1 else None
        if row is None or row["source"] != PERSONAL:
            return
        dlg = ContactDialog(self._folders(), row=row, parent=self)
        if dlg.exec():
            self.saveRequested.emit(dlg.values(), row["id"], row["folder_id"] or "",
                                    row["folder_name"] or "Contacts")

    def copy_to_contacts(self):
        keys = self.selected_keys()
        if len(keys) != 1:
            return
        kind, key = keys[0]
        if kind == "recent":
            r = next((r for r in self.store.recent_recipients(key) if r["address"] == key), None)
            prefill = {"name": (r["name"] if r else "") or "", "address": key}
        else:
            c = self.store.contact_row(key)
            emails = json.loads(c["emails"] or "[]") if c else []
            prefill = {"name": c["display_name"], "address": emails[0][1] if emails else "",
                       "company": c["company"], "job_title": c["job_title"],
                       "department": c["department"]} if c else {}
        self.new_contact(prefill)

    def delete_contact(self):
        keys = self.selected_keys()
        if len(keys) == 1 and keys[0][0] == "contact":
            self.deleteRequested.emit(keys[0][1])

    def select_contact(self, contact_id: str):
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if item.data(0, KEY_ROLE) == ("contact", contact_id):
                self._select(item)
                return

