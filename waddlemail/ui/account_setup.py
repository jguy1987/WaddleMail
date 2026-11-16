"""Adding an account: what kind it is, and its address.

AccountForm is the choice itself: Microsoft 365 (a work or school account, signed in through
the browser: the address typed here is filled in on the sign-in page, so it's one step less)
or IMAP/SMTP (other providers; coming later). AddAccountDialog shows it for File > Add
account; WelcomeWizard shows it on the first run, when there's no account yet.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QLabel, QLineEdit, QRadioButton, QVBoxLayout,
    QWidget, QWizard, QWizardPage,
)

from .. import desktop

M365, IMAP = "m365", "imap"
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    return label


class AccountForm(QWidget):
    """The kind of account, and (for Microsoft 365) its address."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("<b>What kind of account is it?</b>"))
        self.kinds = QButtonGroup(self)
        self.m365 = QRadioButton("Microsoft 365 / Exchange Online (work or school account)")
        self.m365.setChecked(True)
        self.imap = QRadioButton("IMAP / SMTP (Gmail, Yahoo, iCloud, your provider…)")
        self.imap.setEnabled(False)
        self.imap.setToolTip("Coming in a later version")
        for i, button in enumerate((self.m365, self.imap)):
            self.kinds.addButton(button, i)
            lay.addWidget(button)
        soon = _note("<i>IMAP / SMTP accounts are coming in a later version.</i>")
        soon.setIndent(24)
        soon.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(soon)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Email address:"))
        self.address = QLineEdit()
        self.address.setPlaceholderText("you@contoso.com")
        self.address.setClearButtonEnabled(True)
        lay.addWidget(self.address)
        self.problem = QLabel()
        self.problem.setStyleSheet("color: #c42b1c")
        self.problem.hide()
        lay.addWidget(self.problem)
        lay.addWidget(_note(
            "Your browser opens Microsoft's sign-in page with this address filled in; sign in "
            "there (with your password, and any second step your organization uses), then "
            "come back here. WaddleMail never sees your password. You can leave the address "
            "empty to choose the account in the browser."))
        lay.addStretch()
        self.address.textChanged.connect(lambda _: self.problem.hide())

    def kind(self) -> str:
        return M365 if self.m365.isChecked() else IMAP

    def email(self) -> str:
        return self.address.text().strip()

    def check(self) -> bool:
        """Whether it can go ahead (an address, if typed, must look like one)."""
        if self.email() and not _ADDRESS.match(self.email()):
            self.problem.setText("That doesn't look like an email address.")
            self.problem.show()
            self.address.setFocus()
            return False
        return True

    def account(self) -> dict:
        return {"kind": self.kind(), "email": self.email()}


class AddAccountDialog(QDialog):
    """File > Add account…"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add account")
        self.setMinimumSize(520, 400)  # (room for the wrapped notes, and a problem)
        lay = QVBoxLayout(self)
        self.form = AccountForm()
        lay.addWidget(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Sign in", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.form.address.setFocus()

    def accept(self):
        if self.form.check():
            super().accept()

    def account(self) -> dict:
        return self.form.account()


class _AccountPage(QWizardPage):
    def __init__(self, form: AccountForm):
        super().__init__()
        self.form = form
        self.setTitle("Your account")
        self.setSubTitle("Add the account you'd like to read mail from. You can add more "
                         "later with File > Add account.")
        lay = QVBoxLayout(self)
        lay.addWidget(form)

    def validatePage(self) -> bool:
        return self.form.check()


class WelcomeWizard(QWizard):
    """The first run: a welcome, then the account."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to WaddleMail")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        logo = QPixmap(str(desktop.icon_png(64)))
        self.setPixmap(QWizard.WizardPixmap.LogoPixmap, logo)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Sign in")
        self.setMinimumSize(560, 440)

        welcome = QWizardPage()
        welcome.setTitle("Welcome to WaddleMail")
        welcome.setSubTitle("Mail, calendar, contacts and tasks for Microsoft 365.")
        lay = QVBoxLayout(welcome)
        intro = _note(
            "WaddleMail keeps your mail on this computer so it opens quickly and works "
            "offline, and keeps it in step with the server.<br><br>Let's add your first "
            "account. It takes a minute.")
        font = QFont(intro.font())
        font.setPointSizeF(font.pointSizeF() * 1.1)
        intro.setFont(font)
        lay.addWidget(intro)
        lay.addStretch()
        self.addPage(welcome)

        self.form = AccountForm()
        self.addPage(_AccountPage(self.form))

    def account(self) -> dict:
        return self.form.account()
