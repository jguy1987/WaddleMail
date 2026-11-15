"""Editors for the Contacts view: a personal contact (with photo) and a contact group, plus the
avatar drawing they share with the details card."""
from __future__ import annotations

import json
from email.utils import parseaddr

from PySide6.QtCore import QBuffer, QByteArray, QDate, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..contacts import ADDRESS_PARTS, format_address
from . import iconset
from .recipients import RecipientEdit, split_addresses

_AVATAR_COLORS = ["#4f8cff", "#2f9e44", "#e8590c", "#ae3ec9", "#1098ad", "#d6336c", "#5c7cfa"]
MEMBER_ROLE = Qt.ItemDataRole.UserRole


def initials(name: str) -> str:
    parts = [p for p in name.replace(",", " ").split() if p[:1].isalnum()]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def avatar(name: str, size: int = 64, photo: bytes | None = None) -> QImage:
    """The person's photo cropped to a circle, or a coloured circle with their initials (the
    colour follows the name)."""
    img = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32)
    img.setDevicePixelRatio(2)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    circle = QRectF(0, 0, size, size)
    picture = QImage.fromData(photo) if photo else QImage()
    if not picture.isNull():
        side = min(picture.width(), picture.height())  # centre square crop
        square = picture.copy((picture.width() - side) // 2, (picture.height() - side) // 2,
                              side, side)
        clip = QPainterPath()
        clip.addEllipse(circle)
        p.setClipPath(clip)
        p.drawImage(circle, square)
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_AVATAR_COLORS[sum(map(ord, name)) % len(_AVATAR_COLORS)]))
        p.drawEllipse(circle)
        font = QFont()
        font.setPixelSize(int(size * 0.38))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(circle, Qt.AlignmentFlag.AlignCenter, initials(name))
    p.end()
    return img


def photo_from_file(path: str, max_side: int = 512) -> bytes | None:
    """Load an image file, scale it down, and return it as JPEG bytes (None if unreadable)."""
    img = QImage(path)
    if img.isNull():
        return None
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.convertToFormat(QImage.Format.Format_RGB32).save(buf, "JPEG", 90)
    buf.close()
    return bytes(data)


class _AddressEdit(QWidget):
    def __init__(self):
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.street = QPlainTextEdit()
        self.street.setFixedHeight(48)
        self.city, self.state, self.postal, self.country = (QLineEdit() for _ in range(4))
        form.addRow("Street:", self.street)
        form.addRow("City:", self.city)
        form.addRow("State / region:", self.state)
        form.addRow("Postal code:", self.postal)
        form.addRow("Country:", self.country)

    def set(self, parts: dict):
        self.street.setPlainText(parts.get("street") or "")
        self.city.setText(parts.get("city") or "")
        self.state.setText(parts.get("state") or "")
        self.postal.setText(parts.get("postalCode") or "")
        self.country.setText(parts.get("countryOrRegion") or "")

    def value(self) -> dict:
        values = (self.street.toPlainText(), self.city.text(), self.state.text(),
                  self.postal.text(), self.country.text())
        return {k: v.strip() for k, v in zip(ADDRESS_PARTS, values)}


class ContactDialog(QDialog):
    """New / edit a personal contact. values() gives Graph contact fields; photo_bytes is a new
    photo (JPEG) or None when unchanged."""

    def __init__(self, folders: list[tuple[str, str]], row=None, prefill: dict | None = None,
                 photo: bytes | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit contact" if row else "New contact")
        self.setMinimumWidth(520)
        self.photo_bytes: bytes | None = None
        self._photo = photo
        outer = QVBoxLayout(self)

        head = QHBoxLayout()  # photo + name
        self.picture = QLabel()
        self.picture.setFixedSize(72, 72)
        change = QPushButton("Change photo…")
        change.clicked.connect(self._choose_photo)
        pic = QVBoxLayout()
        pic.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignHCenter)
        pic.addWidget(change)
        head.addLayout(pic)
        names = QFormLayout()
        self.given, self.surname, self.display = QLineEdit(), QLineEdit(), QLineEdit()
        self.display.setPlaceholderText("First and last name")
        names.addRow("First name:", self.given)
        names.addRow("Last name:", self.surname)
        names.addRow("Display as:", self.display)
        head.addLayout(names, 1)
        outer.addLayout(head)
        for edit in (self.given, self.surname, self.display):
            edit.textChanged.connect(lambda _: self._show_picture())

        tabs = QTabWidget()
        general = QWidget()
        form = QFormLayout(general)
        self.emails = [QLineEdit() for _ in range(3)]
        for i, edit in enumerate(self.emails):
            form.addRow("Email:" if i == 0 else "", edit)
        self.company, self.title, self.department = QLineEdit(), QLineEdit(), QLineEdit()
        self.work, self.mobile, self.home = QLineEdit(), QLineEdit(), QLineEdit()
        form.addRow("Company:", self.company)
        form.addRow("Job title:", self.title)
        form.addRow("Department:", self.department)
        form.addRow("Work phone:", self.work)
        form.addRow("Mobile:", self.mobile)
        form.addRow("Home phone:", self.home)
        tabs.addTab(general, "General")

        addresses = QWidget()
        alay = QVBoxLayout(addresses)
        self.work_address, self.home_address = _AddressEdit(), _AddressEdit()
        alay.addWidget(QLabel("<b>Work</b>"))
        alay.addWidget(self.work_address)
        alay.addWidget(QLabel("<b>Home</b>"))
        alay.addWidget(self.home_address)
        tabs.addTab(addresses, "Addresses")

        other = QWidget()
        form = QFormLayout(other)
        self.nickname, self.website = QLineEdit(), QLineEdit()
        self.has_birthday = QCheckBox()
        self.birthday = QDateEdit()
        self.birthday.setCalendarPopup(True)
        self.birthday.setDisplayFormat("yyyy-MM-dd")
        self.birthday.setDate(QDate(1990, 1, 1))
        self.birthday.setEnabled(False)
        self.has_birthday.toggled.connect(self.birthday.setEnabled)
        bday = QHBoxLayout()
        bday.addWidget(self.has_birthday)
        bday.addWidget(self.birthday, 1)
        self.notes = QPlainTextEdit()
        form.addRow("Nickname:", self.nickname)
        form.addRow("Birthday:", bday)
        form.addRow("Website:", self.website)
        form.addRow("Notes:", self.notes)
        tabs.addTab(other, "Other")
        outer.addWidget(tabs)

        self.folder = QComboBox()
        for folder_id, name in folders:
            self.folder.addItem(name, folder_id)
        if row is None and len(folders) > 1:
            where = QFormLayout()
            where.addRow("Save in:", self.folder)
            outer.addLayout(where)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        outer.addWidget(box)

        if row is not None:
            self._load(row)
        elif prefill:
            self._prefill(prefill)
        self._show_picture()
        self.given.setFocus()

    # -- photo ------------------------------------------------------------------
    def _show_picture(self):
        name = self.display.text() or f"{self.given.text()} {self.surname.text()}".strip()
        self.picture.setPixmap(QPixmap.fromImage(avatar(name or "?", 72,
                                                        self.photo_bytes or self._photo)))

    def _choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a photo", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if path:
            data = photo_from_file(path)
            if data:
                self.photo_bytes = data
                self._show_picture()

    # -- load / save ------------------------------------------------------------------
    def _prefill(self, prefill: dict):
        self.display.setText(prefill.get("name", ""))
        parts = prefill.get("name", "").split()
        if len(parts) >= 2:
            self.given.setText(" ".join(parts[:-1]))
            self.surname.setText(parts[-1])
        self.emails[0].setText(prefill.get("address", ""))
        self.company.setText(prefill.get("company") or "")
        self.title.setText(prefill.get("job_title") or "")
        self.department.setText(prefill.get("department") or "")

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
        for label, parts in json.loads(row["addresses"] or "[]"):
            (self.work_address if label == "Work" else self.home_address).set(parts)
        self.nickname.setText(row["nickname"] or "")
        self.website.setText(row["website"] or "")
        if row["birthday"]:
            date = QDate.fromString(row["birthday"], "yyyy-MM-dd")
            if date.isValid():
                self.has_birthday.setChecked(True)
                self.birthday.setDate(date)
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
        birthday = None
        if self.has_birthday.isChecked():  # noon UTC, so no time zone moves it to another day
            birthday = self.birthday.date().toString("yyyy-MM-dd") + "T11:59:00Z"
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
            "businessAddress": self.work_address.value(),
            "homeAddress": self.home_address.value(),
            "nickName": self.nickname.text().strip(),
            "businessHomePage": self.website.text().strip(),
            "birthday": birthday,
            "personalNotes": self.notes.toPlainText().strip(),
        }


class GroupDialog(QDialog):
    """New / edit a contact group: name, notes and members. Members are typed with the same
    completion as To/Cc (people, contacts, other groups' members, the directory)."""

    def __init__(self, suggestions: list, group=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit contact group" if group else "New contact group")
        self.setMinimumSize(480, 480)
        # where a typed address came from decides how it is added (contact / mailbox / oneOff)
        self._sources = {}
        for s in suggestions:
            if s.kind != "group":
                for text in s.addresses:
                    self._sources.setdefault(parseaddr(text)[1].lower(), s.kind)
        form = QFormLayout(self)
        self.name = QLineEdit(group["name"] if group else "")
        self.notes = QPlainTextEdit(group["notes"] or "" if group else "")
        self.notes.setFixedHeight(60)
        form.addRow("Name:", self.name)
        form.addRow("Notes:", self.notes)
        self.members = QListWidget()
        self.members.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        form.addRow("Members:", self.members)
        for m in json.loads(group["members"] or "[]") if group else []:
            self._add_item(m)
        add_row = QHBoxLayout()
        self.add_edit = RecipientEdit(suggestions)
        self.add_edit.setPlaceholderText("Type a name or address to add")
        self.add_edit.returnPressed.connect(self._add_typed)
        add = QPushButton(iconset.load("user-plus"), "Add")
        add.clicked.connect(self._add_typed)
        remove = QPushButton(iconset.load("trash-2"), "Remove")
        remove.clicked.connect(self._remove_selected)
        add_row.addWidget(self.add_edit, 1)
        add_row.addWidget(add)
        add_row.addWidget(remove)
        form.addRow("", add_row)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        form.addRow(box)
        self.original = [self.members.item(i).data(MEMBER_ROLE)
                         for i in range(self.members.count())]
        self.name.setFocus()

    def _add_item(self, member: list):
        item = QListWidgetItem(format_address(member[0], member[1]) if member[1]
                               else f"{member[0]} (no address)")
        item.setData(MEMBER_ROLE, member)
        self.members.addItem(item)

    def _addresses_present(self) -> set[str]:
        return {(self.members.item(i).data(MEMBER_ROLE)[1] or "").lower()
                for i in range(self.members.count())}

    def _add_typed(self):
        present = self._addresses_present()
        for text in split_addresses(self.add_edit.text()):
            name, address = parseaddr(text)
            if address and address.lower() not in present:
                present.add(address.lower())
                # new: no key/memberId yet; the type says how to add it
                self._add_item([name or address, address,
                                self._sources.get(address.lower(), "oneOff"), "", ""])
        self.add_edit.clear()

    def _remove_selected(self):
        for item in self.members.selectedItems():
            self.members.takeItem(self.members.row(item))

    def _accept(self):
        self._add_typed()  # an address still in the box counts too
        if self.name.text().strip():
            self.accept()

    def current_members(self) -> list[list]:
        return [self.members.item(i).data(MEMBER_ROLE) for i in range(self.members.count())]

    def changes(self) -> tuple[list[list], list[list]]:
        """(added, removed) compared with when the dialog opened."""
        before = {_member_key(m) for m in self.original}
        after = {_member_key(m) for m in self.current_members()}
        added = [m for m in self.current_members() if _member_key(m) not in before]
        removed = [m for m in self.original if _member_key(m) not in after]
        return added, removed


def _member_key(m: list) -> str:
    """A member's identity: its address, else its key, else its name."""
    return (m[1] or (m[3] if len(m) > 3 else "") or m[0]).lower()
