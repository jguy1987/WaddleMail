"""The compose window: new mail, replies and forwards, as Outlook lays them out.

From (with several accounts), To, Cc and Bcc (Cc/Bcc shown on demand), Subject, and a WYSIWYG
body that can switch to plain text. A reply or forward starts with the cursor at the top,
then your signature, then a line and Outlook's header block (From / Sent / To / Cc / Subject)
over the original message. "Insert signature" puts the signature just above the quoted text
(at the end of a new message). Pictures can be inserted, pasted, or dropped on the body;
other files dropped anywhere are attached.

The window only collects; MainWindow sends and saves drafts (sendRequested / draftRequested)
and closes it when sending worked.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QToolBar, QVBoxLayout,
    QWidget,
)

from ..attachments import human_size
from ..compose import (
    FORWARD, NEW, Outgoing, format_sent, quote_html, quote_text, reply_recipients,
    reply_subject,
)
from ..contacts import format_address
from ..inline import extract_inline
from ..text import body_text
from . import iconset
from .attachment_bar import AttachmentBar
from .recipients import RecipientEdit
from .render import quotable_html
from .rich_editor import RichEditor, is_picture

LARGE_MESSAGE = 35 * 1024 * 1024  # Exchange Online's usual limit for a whole message


class ComposeWindow(QMainWindow):
    sendRequested = Signal(object)    # this window
    draftRequested = Signal(object)

    def __init__(self, accounts: list[tuple[str, str]], suggestions, defaults: dict,
                 signature, kind: str = NEW, original=None, original_body=None,
                 to: list[str] | None = None, account_id: str | None = None,
                 original_attachments: list | None = None, parent=None):
        """`accounts`: [(account id, address)] you can send from; `signature(account id)` ->
        a signatures row or None; `defaults`: html, font, size, color (Preferences);
        `original`: the stored message replied to / forwarded, with `original_body`
        (content type, content) and, for a forward, `original_attachments` (its attachments
        as Attachment objects; None if they couldn't be listed: then they're all kept)."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.kind, self.original = kind, original
        self._signature = signature
        self.sent = False
        self.draft_id: str | None = None  # once saved to Drafts
        self.had_pictures = False  # a saved draft has had inline pictures (see MainWindow)
        self.resize(820, 700)
        me = {a for _, a in accounts}

        bar = QToolBar("Message")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.act_send = bar.addAction(iconset.load("send"), "Send", self._send)
        self.act_send.setShortcut(QKeySequence("Ctrl+Return"))
        self.act_send.setToolTip("Send (Ctrl+Enter)")
        self.act_draft = bar.addAction(iconset.load("save"), "Save draft",
                                       lambda: self.draftRequested.emit(self))
        self.act_draft.setShortcut(QKeySequence.StandardKey.Save)
        bar.addSeparator()
        self.act_attach = bar.addAction(iconset.load("paperclip"), "Attach",
                                        lambda: self.attachments.choose_files())
        self.act_attach.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.act_attach.setToolTip("Attach files (Ctrl+Shift+A); or drop them on the window")
        self.act_signature = bar.addAction(iconset.load("signature"), "Insert signature",
                                           self.insert_signature)
        bar.addSeparator()
        self.act_cc = bar.addAction("Cc")
        self.act_bcc = bar.addAction("Bcc")
        for act in (self.act_cc, self.act_bcc):
            act.setCheckable(True)
        self.act_plain = bar.addAction(iconset.load("type"), "Plain text")
        self.act_plain.setCheckable(True)
        self.act_plain.setToolTip("Write this message as plain text (no formatting)")
        self.addToolBar(bar)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
        form = QFormLayout()
        self.sender = QComboBox()
        for account_id_, address in accounts:
            self.sender.addItem(address, account_id_)
        if account_id:
            self.sender.setCurrentIndex(max(self.sender.findData(account_id), 0))
        self.sender.setEnabled(kind == NEW)  # a reply goes from the mailbox it was received in
        if len(accounts) > 1:
            form.addRow("From:", self.sender)
        suggestions = list(suggestions)
        self.to, self.cc, self.bcc = (RecipientEdit(suggestions) for _ in range(3))
        self.subject = QLineEdit()
        form.addRow("To:", self.to)
        self.cc_label, self.bcc_label = QLabel("Cc:"), QLabel("Bcc:")
        form.addRow(self.cc_label, self.cc)
        form.addRow(self.bcc_label, self.bcc)
        form.addRow("Subject:", self.subject)
        self.attachments = AttachmentBar()
        self.attachments.changed.connect(self._attachment_change)
        form.addRow("", self.attachments)
        self._form = form
        self._attachments_changed = False
        # whether attachments have come into it at all (only then do drafts check them)
        self.has_had_attachments = bool(original_attachments) or (
            kind == FORWARD and original_attachments is None)
        # A forward carries the original's attachments: listed so you can take some out.
        self.originals_known = original_attachments is not None
        if original_attachments:
            self.attachments.add(original_attachments)
            self._attachments_changed = False
        lay.addLayout(form)
        self.editor = RichEditor()
        lay.addWidget(self.editor, 1)
        self.setAcceptDrops(True)  # files dropped anywhere (the body too) become attachments
        self.editor.edit.viewport().installEventFilter(self)
        self.setCentralWidget(central)
        self.act_cc.toggled.connect(lambda on: self._show_row(self.cc_label, self.cc, on))
        self.act_bcc.toggled.connect(lambda on: self._show_row(self.bcc_label, self.bcc, on))
        self.act_plain.toggled.connect(self._plain_toggled)
        self.subject.textChanged.connect(self._title)

        # what goes where
        if original is not None:
            t, c = reply_recipients(original, kind, me)
            self.to.setText("; ".join(format_address(n, a) for n, a in t) + ("; " if t else ""))
            self.cc.setText("; ".join(format_address(n, a) for n, a in c) + ("; " if c else ""))
            self.subject.setText(reply_subject(original["subject"], kind))
        elif to:
            self.to.setText("; ".join(to) + "; ")
        self.act_cc.setChecked(bool(self.cc.text().strip()))
        self._show_row(self.cc_label, self.cc, self.act_cc.isChecked())
        self._show_row(self.bcc_label, self.bcc, False)

        self.editor.set_default_font(defaults.get("font", "Calibri"),
                                     int(defaults.get("size", 11)),
                                     defaults.get("color", "#000000"))
        self._quote_at: QTextCursor | None = None  # follows the start of the quoted text
        self._fill_body(original_body, defaults.get("html", True))
        if not defaults.get("html", True):
            self.act_plain.setChecked(True)
        self.editor.edit.document().setModified(False)
        self._title()
        (self.to if not self.to.text().strip() else
         (self.subject if not self.subject.text().strip() else self.editor.edit)).setFocus()

    # -- body ------------------------------------------------------------------------------
    def _fill_body(self, original_body, as_html: bool):
        """[cursor] / signature (if automatic for this kind of message) / quoted original."""
        edit = self.editor.edit
        sig = self._auto_signature()
        cursor = edit.textCursor()
        if sig:  # a line to write on, then the signature
            cursor.insertBlock()
            cursor.insertHtml(sig)
        if self.original is not None and original_body is not None:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertBlock()
            start = cursor.position()
            sent = format_sent(self.original["received"])
            ctype, content = original_body
            if as_html:
                cursor.insertHtml(quote_html(self.original, sent,
                                             quotable_html(ctype, content)))
            else:
                cursor.insertText(quote_text(self.original, sent, body_text(ctype, content)))
            # Marks where the quote starts, and moves along as text is typed above it. (Made
            # after inserting: a cursor at an insertion point ends up after the new text.)
            self._quote_at = QTextCursor(edit.document())
            self._quote_at.setPosition(start)
        edit.moveCursor(QTextCursor.MoveOperation.Start)

    def _signature_html(self) -> str:
        row = self._signature(self.sender.currentData())
        return row["html"] if row and row["html"] and _has_text(row["html"]) else ""

    def _auto_signature(self) -> str:
        row = self._signature(self.sender.currentData())
        if not row or not _has_text(row["html"] or ""):
            return ""
        wanted = row["on_new"] if self.kind == NEW else row["on_reply"]
        return _fragment(row["html"]) if wanted else ""

    def insert_signature(self):
        """Put the signature just above the quoted text, or at the end of a new message."""
        html_sig = self._signature_html()
        if not html_sig:
            QMessageBox.information(self, "Signature", "There's no signature for this account "
                                    "yet. Add one in Preferences > Signatures.")
            return
        edit = self.editor.edit
        cursor = QTextCursor(edit.document())
        if self._quote_at is not None:
            cursor.setPosition(self._quote_at.position())
            cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        if self.editor.is_plain():
            cursor.insertText(body_text("html", html_sig))
        else:
            cursor.insertHtml(_fragment(html_sig))

    def _plain_toggled(self, plain: bool):
        if plain and self.editor.edit.document().isModified() and not self.editor.is_plain():
            if QMessageBox.question(self, "Plain text", "Switch to plain text? Formatting in "
                                    "this message is removed.") != QMessageBox.StandardButton.Yes:
                self.act_plain.blockSignals(True)
                self.act_plain.setChecked(False)
                self.act_plain.blockSignals(False)
                return
        self.editor.set_plain(plain)

    # -- fields ------------------------------------------------------------------------------
    def _show_row(self, label, field, on: bool):
        label.setVisible(on)
        field.setVisible(on)
        if on:
            field.setFocus()

    def _title(self, *_):
        what = {NEW: "New message", FORWARD: "Forward"}.get(self.kind, "Reply")
        self.setWindowTitle(self.subject.text().strip() or what)

    def account_id(self) -> str:
        return self.sender.currentData() or ""

    def outgoing(self) -> Outgoing:
        plain = self.editor.is_plain()
        body, inline = (self.editor.text(), []) if plain else extract_inline(self.editor.html())
        return Outgoing(subject=self.subject.text().strip(), body=body, html=not plain,
                        to=self.to.addresses(), cc=self.cc.addresses(),
                        bcc=self.bcc.addresses(), inline=inline)

    def is_dirty(self) -> bool:
        return (self.editor.edit.document().isModified() or self._attachments_changed or
                (self.kind == NEW and bool(self.to.text().strip() or self.subject.text().strip())))

    # -- send / close ------------------------------------------------------------------------
    def _send(self):
        out = self.outgoing()
        if not out.has_recipients():
            QMessageBox.warning(self, "Send", "Who is this for? Add someone in To, Cc or Bcc.")
            self.to.setFocus()
            return
        if not out.subject and QMessageBox.question(
                self, "Send", "Send this message without a subject?") != \
                QMessageBox.StandardButton.Yes:
            self.subject.setFocus()
            return
        total = self.attachments.total_size() + sum(len(i.data) for i in out.inline)
        if total > LARGE_MESSAGE and QMessageBox.question(
                self, "Send", f"Attachments and pictures add up to {human_size(total)}. Most "
                "mail servers don't accept messages over about 35 MB, so it may come back. "
                "Send anyway?") != QMessageBox.StandardButton.Yes:
            return
        self.sendRequested.emit(self)

    def _attachment_change(self):
        self._attachments_changed = True
        self.has_had_attachments = True

    def mark_saved(self, draft_id: str):
        self.draft_id = draft_id
        self.editor.edit.document().setModified(False)
        self._attachments_changed = False

    # -- dropping files --------------------------------------------------------------------
    @staticmethod
    def _files(mime) -> list[str]:
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() \
            else []

    def dragEnterEvent(self, event):
        if self._files(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self._drop(self._files(event.mimeData()))
        event.acceptProposedAction()

    def eventFilter(self, obj, event):
        # The body would paste a dropped file's path as text: pictures go into the text where
        # they're dropped, anything else is attached.
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and \
                self._files(event.mimeData()):
            event.acceptProposedAction()
            return True
        if event.type() == QEvent.Type.Drop and self._files(event.mimeData()):
            files = self._files(event.mimeData())
            if not self.editor.is_plain() and all(is_picture(p) for p in files):
                cursor = self.editor.edit.cursorForPosition(event.position().toPoint())
                for path in files:
                    self.editor.insert_picture_file(path, cursor)
            else:
                self._drop(files)
            event.acceptProposedAction()
            return True
        return super().eventFilter(obj, event)

    def _drop(self, paths: list[str]):
        refused = self.attachments.add_files(paths)
        if refused:
            QMessageBox.warning(self, "Attach", "Not attached:\n" + "\n".join(refused))

    def closeEvent(self, event):
        if self.sent or not self.is_dirty():
            super().closeEvent(event)
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Close message",
                          "Save this message to Drafts?", parent=self)
        save = box.addButton("Save draft", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is save:
            self.sent = True  # (closes once the draft is saved; see MainWindow)
            self.draftRequested.emit(self)
            event.ignore()
        elif box.clickedButton() is discard:
            self.sent = True
            super().closeEvent(event)
        else:
            event.ignore()


def _has_text(signature_html: str) -> bool:
    return bool(body_text("html", signature_html).strip()) or "<img" in signature_html


def _fragment(doc_html: str) -> str:
    """The inside of <body> of a QTextEdit HTML document (to insert it elsewhere)."""
    lower = doc_html.lower()
    start = lower.find("<body")
    if start < 0:
        return doc_html
    start = lower.find(">", start) + 1
    end = lower.rfind("</body>")
    return doc_html[start:end if end > 0 else None]

