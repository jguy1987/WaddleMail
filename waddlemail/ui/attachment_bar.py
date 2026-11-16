"""The attachments of a message or event being written: a row of files with their sizes, a
button to add more (files can also be dropped on the window), Delete or the × to take one
out, and the total."""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QToolButton, QWidget,
)

from ..attachments import Attachment, human_size
from ..graph import GraphClient
from . import iconset

ROLE = Qt.ItemDataRole.UserRole


class AttachmentBar(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.add_button = QToolButton()
        self.add_button.setIcon(iconset.load("paperclip"))
        self.add_button.setText("Attach…")
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_button.setToolTip("Attach files (you can also drop files here)")
        self.add_button.clicked.connect(self.choose_files)
        self.list = QListWidget()
        self.list.setFlow(QListWidget.Flow.LeftToRight)
        self.list.setWrapping(True)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setSpacing(2)
        self.list.setIconSize(QSize(16, 16))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setMaximumHeight(64)
        self.list.setToolTip("Delete removes the selected attachment")
        self.remove_button = QToolButton()
        self.remove_button.setIcon(iconset.load("x"))
        self.remove_button.setToolTip("Remove the selected attachment")
        self.remove_button.clicked.connect(self.remove_selected)
        self.total = QLabel()
        self.total.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(self.add_button)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.remove_button)
        lay.addWidget(self.total)
        self._refresh()

    # -- contents ------------------------------------------------------------------------
    def items(self) -> list[Attachment]:
        return [self.list.item(i).data(ROLE) for i in range(self.list.count())]

    def add(self, attachments: list[Attachment]) -> None:
        for att in attachments:
            item = QListWidgetItem(iconset.load("paperclip"),
                                   f"{att.name}  ({human_size(att.size)})")
            item.setData(ROLE, att)
            item.setToolTip(att.path or "Already attached")
            self.list.addItem(item)
        self._refresh()
        self.changed.emit()

    def add_files(self, paths: list[str]) -> list[str]:
        """Add files; returns the ones refused (too big, unreadable)."""
        refused, ok = [], []
        for path in paths:
            if not os.path.isfile(path):
                continue
            att = Attachment.from_file(path)
            if att.size > GraphClient.MAX_ATTACHMENT:
                refused.append(f"{att.name} ({human_size(att.size)}): larger than 150 MB")
            else:
                ok.append(att)
        self.add(ok)
        return refused

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        refused = self.add_files(paths)
        if refused:
            QMessageBox.warning(self, "Attach", "Not attached:\n" + "\n".join(refused))

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._refresh()
        self.changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected()
            return
        super().keyPressEvent(event)

    def total_size(self) -> int:
        return sum(a.size for a in self.items())

    def _refresh(self) -> None:
        n = self.list.count()
        self.list.setVisible(n > 0)
        self.remove_button.setVisible(n > 0)
        self.total.setText(f"{n} file{'s' if n != 1 else ''}, {human_size(self.total_size())}"
                           if n else "")
