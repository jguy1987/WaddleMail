"""A WYSIWYG editor: a white page (as the recipient sees it, whatever the app theme) with a
formatting bar: font, size, bold, italic, underline, strikethrough, colour, highlight,
bullets, numbering, indent, alignment, link, picture and clear formatting. It can switch to
plain text. Used for composing mail and for signatures.

Pictures (inserted, pasted or dropped) are held in the document as data: URIs; sending turns
them into inline attachments (inline.extract_inline)."""
from __future__ import annotations

import mimetypes
import os

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QUrl
from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QPalette, QTextBlockFormat, QTextCharFormat, QTextCursor,
    QTextImageFormat, QTextListFormat,
)
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFileDialog, QFontComboBox, QInputDialog, QMessageBox, QTextEdit,
    QToolBar, QVBoxLayout, QWidget,
)

from ..inline import data_uri
from . import iconset

SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72]
PICTURE_WIDTH = 640                  # wider pictures go in at this width (the file is kept)
MAX_PICTURE = 10 * 1024 * 1024       # bigger ones should be attached instead
PICTURE_FILES = "Pictures (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"


def is_picture(path: str) -> bool:
    kind, _ = mimetypes.guess_type(path)
    return bool(kind) and kind.startswith("image/") and kind != "image/svg+xml"


class _Edit(QTextEdit):
    """Pasting a picture (or picture files) puts it in the text."""

    def __init__(self, owner: "RichEditor"):
        super().__init__()
        self._owner = owner

    def canInsertFromMimeData(self, source) -> bool:
        if not self._owner.is_plain() and (source.hasImage() or self._pictures(source)):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        if not self._owner.is_plain():
            files = self._pictures(source)
            if files:
                for path in files:
                    self._owner.insert_picture_file(path)
                return
            if source.hasImage() and not source.hasHtml():  # (HTML keeps its own pictures)
                image = QImage(source.imageData())
                if not image.isNull():
                    data = QByteArray()
                    buf = QBuffer(data)
                    buf.open(QIODevice.OpenModeFlag.WriteOnly)
                    image.save(buf, "PNG")
                    self._owner.insert_picture(bytes(data), "image/png")
                    return
        super().insertFromMimeData(source)

    @staticmethod
    def _pictures(source) -> list[str]:
        if not source.hasUrls():
            return []
        paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
        return paths if paths and all(is_picture(p) for p in paths) else []


class RichEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.bar = QToolBar("Format")
        self.bar.setIconSize(QSize(16, 16))
        self.edit = _Edit(self)
        self.edit.setAcceptRichText(True)
        self.edit.setTabChangesFocus(False)
        # a white page with black text, like the message will look (dark theme or not)
        pal = self.edit.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor("white"))
        pal.setColor(QPalette.ColorRole.Text, QColor("black"))
        self.edit.setPalette(pal)
        lay.addWidget(self.bar)
        lay.addWidget(self.edit, 1)
        self._plain = False
        self._default_color = QColor("black")

        self.font_box = QFontComboBox()
        self.font_box.setMaximumWidth(180)
        self.font_box.currentFontChanged.connect(
            lambda f: self._merge(lambda fmt: fmt.setFontFamilies([f.family()])))
        self.size_box = QComboBox()
        self.size_box.setEditable(True)
        for size in SIZES:
            self.size_box.addItem(str(size))
        self.size_box.textActivated.connect(self._set_size)
        self.bar.addWidget(self.font_box)
        self.bar.addWidget(self.size_box)
        self.bar.addSeparator()

        def toggle(icon, tip, key, apply):
            act = QAction(iconset.load(icon), tip, self)
            act.setCheckable(True)
            act.setToolTip(f"{tip} ({key})" if key else tip)
            if key:
                act.setShortcut(key)
                act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            act.triggered.connect(lambda on: self._merge(lambda fmt: apply(fmt, on)))
            self.bar.addAction(act)
            self.addAction(act)
            return act

        self.act_bold = toggle("bold", "Bold", "Ctrl+B", lambda f, on: f.setFontWeight(
            QFont.Weight.Bold if on else QFont.Weight.Normal))
        self.act_italic = toggle("italic", "Italic", "Ctrl+I", lambda f, on: f.setFontItalic(on))
        self.act_underline = toggle("underline", "Underline", "Ctrl+U",
                                    lambda f, on: f.setFontUnderline(on))
        self.act_strike = toggle("strikethrough", "Strikethrough", "",
                                 lambda f, on: f.setFontStrikeOut(on))
        self.bar.addSeparator()
        self.bar.addAction(iconset.load("baseline"), "Text colour", self._text_color)
        self.bar.addAction(iconset.load("highlighter"), "Highlight", self._highlight)
        self.bar.addSeparator()
        self.bar.addAction(iconset.load("list"), "Bullets",
                           lambda: self._list(QTextListFormat.Style.ListDisc))
        self.bar.addAction(iconset.load("list-ordered"), "Numbering",
                           lambda: self._list(QTextListFormat.Style.ListDecimal))
        self.bar.addAction(iconset.load("indent-decrease"), "Decrease indent",
                           lambda: self._indent(-1))
        self.bar.addAction(iconset.load("indent-increase"), "Increase indent",
                           lambda: self._indent(1))
        self.bar.addSeparator()
        self._aligns = {}
        for icon, tip, flag in (("align-left", "Align left", Qt.AlignmentFlag.AlignLeft),
                                ("align-center", "Centre", Qt.AlignmentFlag.AlignHCenter),
                                ("align-right", "Align right", Qt.AlignmentFlag.AlignRight),
                                ("align-justify", "Justify", Qt.AlignmentFlag.AlignJustify)):
            act = self.bar.addAction(iconset.load(icon), tip,
                                     lambda f=flag: self.edit.setAlignment(f))
            act.setCheckable(True)
            self._aligns[flag] = act
        self.bar.addSeparator()
        self.bar.addAction(iconset.load("image"), "Insert picture", self.choose_picture)
        act = self.bar.addAction(iconset.load("link"), "Insert link (Ctrl+K)", self.insert_link)
        act.setShortcut("Ctrl+K")
        act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.addAction(act)
        self.bar.addAction(iconset.load("remove-formatting"), "Clear formatting",
                           self.clear_formatting)
        self.edit.currentCharFormatChanged.connect(self._show_format)
        self.edit.cursorPositionChanged.connect(self._show_alignment)

    # -- defaults and modes -------------------------------------------------------------
    def set_default_font(self, family: str, size: int, color: str = "#000000"):
        font = QFont(family, size)
        self.edit.document().setDefaultFont(font)
        self._default_color = QColor(color)
        fmt = QTextCharFormat()
        fmt.setFontFamilies([family])
        fmt.setFontPointSize(size)
        fmt.setForeground(self._default_color)
        self.edit.setCurrentCharFormat(fmt)
        self._show_format(fmt)

    def is_plain(self) -> bool:
        return self._plain

    def set_plain(self, plain: bool):
        """Plain text: formatting is dropped (the text stays)."""
        if plain == self._plain:
            return
        self._plain = plain
        self.bar.setVisible(not plain)
        self.edit.setAcceptRichText(not plain)
        if plain:
            text = self.edit.toPlainText()
            self.edit.setPlainText(text)
        else:
            text = self.edit.toPlainText()
            self.edit.clear()
            cursor = self.edit.textCursor()
            cursor.insertText(text)
            self.edit.moveCursor(QTextCursor.MoveOperation.Start)

    def html(self) -> str:
        return self.edit.toHtml()

    def text(self) -> str:
        return self.edit.toPlainText()

    # -- formatting ------------------------------------------------------------------------
    def _merge(self, change):
        fmt = QTextCharFormat()
        change(fmt)
        cursor = self.edit.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        cursor.mergeCharFormat(fmt)
        self.edit.mergeCurrentCharFormat(fmt)
        self.edit.setFocus()

    def _set_size(self, text: str):
        try:
            size = float(text)
        except ValueError:
            return
        if size > 0:
            self._merge(lambda fmt: fmt.setFontPointSize(size))

    def _text_color(self):
        color = QColorDialog.getColor(self.edit.textColor(), self, "Text colour")
        if color.isValid():
            self._merge(lambda fmt: fmt.setForeground(color))

    def _highlight(self):
        color = QColorDialog.getColor(QColor("#ffff00"), self, "Highlight")
        if color.isValid():
            self._merge(lambda fmt: fmt.setBackground(color))

    def _list(self, style):
        cursor = self.edit.textCursor()
        current = cursor.currentList()
        if current is not None and current.format().style() == style:  # toggle off
            block = cursor.blockFormat()
            current.remove(cursor.block())
            block.setIndent(0)
            cursor.setBlockFormat(block)
            return
        fmt = QTextListFormat()
        fmt.setStyle(style)
        cursor.createList(fmt)

    def _indent(self, step: int):
        cursor = self.edit.textCursor()
        current = cursor.currentList()
        if current is not None:
            fmt = current.format()
            fmt.setIndent(max(fmt.indent() + step, 1))
            current.setFormat(fmt)
            return
        block = QTextBlockFormat(cursor.blockFormat())
        block.setIndent(max(block.indent() + step, 0))
        cursor.setBlockFormat(block)

    def insert_link(self):
        cursor = self.edit.textCursor()
        url, ok = QInputDialog.getText(self, "Insert link", "Address:", text="https://")
        if not ok or not url.strip() or url.strip() == "https://":
            return
        url = url.strip()
        text = cursor.selectedText() or url
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(QUrl.fromUserInput(url).toString())
        fmt.setForeground(QColor("#0563c1"))
        fmt.setFontUnderline(True)
        cursor.insertText(text, fmt)
        cursor.setCharFormat(QTextCharFormat())  # typing after the link isn't part of it

    # -- pictures ----------------------------------------------------------------------------
    def choose_picture(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Insert picture", "", PICTURE_FILES)
        for path in paths:
            self.insert_picture_file(path)

    def insert_picture_file(self, path: str, cursor: QTextCursor | None = None) -> bool:
        try:
            if os.path.getsize(path) > MAX_PICTURE:
                QMessageBox.warning(self, "Insert picture", f"{os.path.basename(path)} is over "
                                    "10 MB: attach it instead.")
                return False
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            QMessageBox.warning(self, "Insert picture", str(exc))
            return False
        kind, _ = mimetypes.guess_type(path)
        return self.insert_picture(data, kind or "image/png", cursor)

    def insert_picture(self, data: bytes, content_type: str,
                       cursor: QTextCursor | None = None) -> bool:
        """Put a picture in the text at the cursor, at most PICTURE_WIDTH wide."""
        image = QImage.fromData(data)
        if self._plain or image.isNull():
            return False
        fmt = QTextImageFormat()
        fmt.setName(data_uri(content_type, data))
        w, h = image.width(), image.height()
        scale = min(1.0, PICTURE_WIDTH / w) if w else 1.0
        fmt.setWidth(round(w * scale))
        fmt.setHeight(round(h * scale))
        (cursor or self.edit.textCursor()).insertImage(fmt)
        self.edit.setFocus()
        return True

    def clear_formatting(self):
        cursor = self.edit.textCursor()
        fmt = QTextCharFormat()
        doc_font = self.edit.document().defaultFont()
        fmt.setFontFamilies([doc_font.family()])
        fmt.setFontPointSize(doc_font.pointSizeF())
        fmt.setForeground(self._default_color)
        if cursor.hasSelection():
            cursor.setCharFormat(fmt)
        self.edit.setCurrentCharFormat(fmt)

    # -- the bar follows the cursor --------------------------------------------------------
    def _show_format(self, fmt: QTextCharFormat):
        families = fmt.fontFamilies() or [self.edit.document().defaultFont().family()]
        self.font_box.blockSignals(True)
        self.font_box.setCurrentFont(QFont(families[0] if isinstance(families, list)
                                           else str(families)))
        self.font_box.blockSignals(False)
        size = fmt.fontPointSize() or self.edit.document().defaultFont().pointSizeF()
        self.size_box.setEditText(f"{size:g}")
        self.act_bold.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.act_italic.setChecked(fmt.fontItalic())
        self.act_underline.setChecked(fmt.fontUnderline())
        self.act_strike.setChecked(fmt.fontStrikeOut())

    def _show_alignment(self):
        align = self.edit.alignment()
        for flag, act in self._aligns.items():
            act.setChecked(bool(align & flag))
