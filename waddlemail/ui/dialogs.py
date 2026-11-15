"""New message dialog (collecting input only; sending is not wired up yet) and the Add shared
mailbox dialog. (Events: see event_editor.py.)"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
)


class _Base(QDialog):
    def _finish(self, form: QFormLayout, accept_label: str) -> None:
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.addButton(accept_label, QDialogButtonBox.ButtonRole.AcceptRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)


class ComposeDialog(_Base):
    def __init__(self, parent=None, suggestions=(), to: list[str] | None = None):
        """`suggestions` (contacts.suggestions()) feed name completion in To and Cc; `to`
        fills in the recipients (from the Contacts view, say)."""
        super().__init__(parent)
        from .recipients import RecipientEdit  # (keeps this module light for the tests)
        self.setWindowTitle("New message")
        self.resize(640, 480)
        form = QFormLayout(self)
        suggestions = list(suggestions)
        self.to, self.cc = RecipientEdit(suggestions), RecipientEdit(suggestions)
        self.subject, self.body = QLineEdit(), QPlainTextEdit()
        if to:
            self.to.setText("; ".join(to) + "; ")
        form.addRow("To:", self.to)
        form.addRow("Cc:", self.cc)
        form.addRow("Subject:", self.subject)
        form.addRow(self.body)
        self._finish(form, "Send")
        (self.subject if to else self.to).setFocus()

    def values(self) -> dict:
        return {"to": self.to.addresses(), "cc": self.cc.addresses(),
                "subject": self.subject.text(), "body": self.body.toPlainText()}


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
