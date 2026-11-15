"""New message / new event dialogs (collecting input only; sending is not wired up yet) and
the Add shared mailbox dialog."""
from __future__ import annotations

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit,
)


def _split(text: str) -> list[str]:
    return [a.strip() for a in text.replace(",", ";").split(";") if a.strip()]


class _Base(QDialog):
    def _finish(self, form: QFormLayout, accept_label: str) -> None:
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.addButton(accept_label, QDialogButtonBox.ButtonRole.AcceptRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)


class ComposeDialog(_Base):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New message")
        self.resize(640, 480)
        form = QFormLayout(self)
        self.to, self.subject, self.body = QLineEdit(), QLineEdit(), QPlainTextEdit()
        form.addRow("To:", self.to)
        form.addRow("Subject:", self.subject)
        form.addRow(self.body)
        self._finish(form, "Send")

    def values(self) -> dict:
        return {"to": _split(self.to.text()), "subject": self.subject.text(),
                "body": self.body.toPlainText()}


class EventDialog(_Base):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New event")
        form = QFormLayout(self)
        self.title, self.location, self.attendees = QLineEdit(), QLineEdit(), QLineEdit()
        now = QDateTime.currentDateTime()
        self.start = QDateTimeEdit(now.addSecs(3600))
        self.end = QDateTimeEdit(now.addSecs(7200))
        for w in (self.start, self.end):
            w.setCalendarPopup(True)
        form.addRow("Title:", self.title)
        form.addRow("Start:", self.start)
        form.addRow("End:", self.end)
        form.addRow("Location:", self.location)
        form.addRow("Attendees:", self.attendees)
        self._finish(form, "Save")

    def values(self) -> dict:
        fmt = "yyyy-MM-ddTHH:mm:ss"
        return {"title": self.title.text(), "location": self.location.text(),
                "start": self.start.dateTime().toString(fmt),
                "end": self.end.dateTime().toString(fmt),
                "attendees": _split(self.attendees.text())}


class SharedMailboxDialog(_Base):
    """Ask for a shared mailbox (or a person whose folders are shared with you)."""

    def __init__(self, accounts: list[tuple[str, str]], parent=None):
        """`accounts` is [(account_id, username)]; the chosen one opens the mailbox."""
        super().__init__(parent)
        self.setWindowTitle("Add shared mailbox")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        intro = QLabel("Open a shared mailbox, or the folders someone has shared with you. "
                       "Your organization must have given you access.")
        intro.setWordWrap(True)
        form.addRow(intro)
        self.account = QComboBox()
        for account_id, username in accounts:
            self.account.addItem(username, account_id)
        if len(accounts) > 1:
            form.addRow("Open with:", self.account)
        self.address = QLineEdit()
        self.address.setPlaceholderText("support@contoso.com")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Optional, defaults to the address")
        form.addRow("Address:", self.address)
        form.addRow("Show as:", self.name)
        self._finish(form, "Add")

    def accept(self):
        if "@" in self.address.text():
            super().accept()
        else:
            self.address.setFocus()

    def values(self) -> dict:
        return {"account_id": self.account.currentData(), "address": self.address.text().strip(),
                "display_name": self.name.text().strip()}
