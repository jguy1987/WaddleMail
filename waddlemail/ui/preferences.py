"""Preferences dialog."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFontComboBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..settings import Settings
from .tray import MODES


class PeriodEdit(QWidget):
    """Number + unit (days / months / all). Stored as a day count; 0 means all."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.spin = QSpinBox()
        self.spin.setRange(1, 999)
        self.unit = QComboBox()
        self.unit.addItems(["days", "months", "all"])
        self.unit.currentTextChanged.connect(lambda t: self.spin.setEnabled(t != "all"))
        lay.addWidget(self.spin)
        lay.addWidget(self.unit)
        lay.addStretch()

    def set_days(self, days: int) -> None:
        if days <= 0:
            self.unit.setCurrentText("all")
        elif days % 30 == 0:
            self.spin.setValue(days // 30)
            self.unit.setCurrentText("months")
        else:
            self.spin.setValue(days)
            self.unit.setCurrentText("days")

    def days(self) -> int:
        unit = self.unit.currentText()
        return 0 if unit == "all" else self.spin.value() * (30 if unit == "months" else 1)


class PreferencesDialog(QDialog):
    def __init__(self, settings: Settings, clear_cache, parent=None, store=None,
                 accounts: list[tuple[str, str]] = ()):
        """`store` and `accounts` ([(account id, address)]) are for the Signatures tab."""
        super().__init__(parent)
        self.s = settings
        self.store = store
        self.setWindowTitle("Preferences")
        self.resize(640, 460)
        tabs = QTabWidget()

        general = QWidget()
        f = QFormLayout(general)
        self.images = QCheckBox("Load remote images by default")
        self.images.setChecked(settings.get("images/load_remote"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setSuffix(" min")
        self.interval.setValue(settings.get("sync/interval_min"))
        f.addRow(self.images)
        f.addRow("Check for new mail every:", self.interval)
        self.notify = QCheckBox("Show a desktop notification when new mail arrives")
        self.notify.setToolTip("For the Inbox of each account you're signed in to")
        self.notify.setChecked(settings.get("notify/new_mail"))
        self.tray = QCheckBox("Show WaddleMail in the system tray")
        self.tray.setChecked(settings.get("tray/show"))
        self.close_to_tray = QCheckBox("Keep running in the tray when the window is closed")
        self.close_to_tray.setChecked(settings.get("tray/close_to_tray"))
        self.tray.toggled.connect(self.close_to_tray.setEnabled)
        self.close_to_tray.setEnabled(self.tray.isChecked())
        self.tray_icon = QComboBox()
        for key, text in MODES:
            self.tray_icon.addItem(text, key)
        self.tray_icon.setCurrentIndex(max(self.tray_icon.findData(settings.get("tray/icon")), 0))
        self.tray_icon.setToolTip("Which colour the tray icon is drawn in. \"Match the theme\" "
                                  "suits most desktops; pick light or dark if the panel "
                                  "doesn't follow the theme.")
        self.tray.toggled.connect(self.tray_icon.setEnabled)
        self.tray_icon.setEnabled(self.tray.isChecked())
        self.splash = QCheckBox("Show the splash screen while starting")
        self.splash.setChecked(settings.get("view/splash"))
        self.reminders = QCheckBox("Show event reminders")
        self.reminders.setChecked(settings.get("reminders/enabled"))
        for box in (self.notify, self.reminders, self.tray, self.close_to_tray):
            f.addRow(box)
        f.addRow("Tray icon:", self.tray_icon)
        f.addRow(self.splash)
        tabs.addTab(general, "General")

        mail = QWidget()
        f = QFormLayout(mail)
        self.sync_window, self.list_window = PeriodEdit(), PeriodEdit()
        self.sync_window.set_days(settings.get("sync/window_days"))
        self.list_window.set_days(settings.get("list/window_days"))
        self.sync_window.setToolTip(
            "Mail received in this period (or the \"Show in message list\" period, if that is "
            "longer) is downloaded and kept up to date. Older mail stays on the server: use "
            "\"Download older mail\" under the message list to get it. Making the period "
            "longer downloads the extra mail on the next sync.")
        f.addRow("Keep mail locally for:", self.sync_window)
        f.addRow("Show in message list:", self.list_window)
        note = QLabel("Mail in the longer of these two periods is downloaded: the sender, "
                      "subject and date, not the message text (that comes when you open a "
                      "message). A shorter \"Show\" period only hides older mail from the list.")
        note.setWordWrap(True)
        note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        f.addRow(note)
        self.mark_read = QCheckBox("Mark messages as read when shown in the reading pane")
        self.mark_read.setChecked(settings.get("read/mark_on_view"))
        self.mark_delay = QSpinBox()
        self.mark_delay.setRange(0, 60)
        self.mark_delay.setSuffix(" s")
        self.mark_delay.setSpecialValueText("immediately")
        self.mark_delay.setValue(settings.get("read/mark_delay_sec"))
        self.mark_read.toggled.connect(self.mark_delay.setEnabled)
        self.mark_delay.setEnabled(self.mark_read.isChecked())
        f.addRow(self.mark_read)
        f.addRow("Mark as read after:", self.mark_delay)
        tabs.addTab(mail, "Mail")

        storage = QWidget()
        f = QFormLayout(storage)
        self.path = QLineEdit(settings.get("storage/path"))
        self.path.setPlaceholderText("Default (~/.local/share/waddlemail)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.path)
        row.addWidget(browse)
        self.cache_mb = QSpinBox()
        self.cache_mb.setRange(10, 100_000)
        self.cache_mb.setSuffix(" MB")
        self.cache_mb.setValue(settings.get("cache/max_mb"))
        self.cache_age = PeriodEdit()
        self.cache_age.set_days(settings.get("cache/max_age_days"))
        clear = QPushButton("Clear cached message bodies")
        clear.clicked.connect(lambda checked=False: clear_cache())
        f.addRow("Data location (applies after restart):", row)
        f.addRow("Body cache size limit:", self.cache_mb)
        f.addRow("Discard cached bodies not opened for:", self.cache_age)
        f.addRow(clear)
        tabs.addTab(storage, "Storage")

        compose = QWidget()
        f = QFormLayout(compose)
        self.html_format, self.text_format = QRadioButton("HTML"), QRadioButton("Plain text")
        (self.html_format if settings.get("compose/html") else self.text_format).setChecked(True)
        fmt = QHBoxLayout()
        fmt.addWidget(self.html_format)
        fmt.addWidget(self.text_format)
        fmt.addStretch()
        f.addRow("Write new mail in:", fmt)
        self.font_box = QFontComboBox()
        self.font_box.setCurrentFont(QFont(settings.get("compose/font")))
        # A font that isn't installed shows as its stand-in (Calibri as Carlito, say); save
        # the stand-in only if you pick a font, so mail keeps naming the font you chose.
        self._shown_font = self.font_box.currentFont().family()
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 72)
        self.font_size.setSuffix(" pt")
        self.font_size.setValue(settings.get("compose/size"))
        self.font_color = QPushButton()
        self._color = QColor(settings.get("compose/color"))
        self.font_color.clicked.connect(self._pick_color)
        font_row = QHBoxLayout()
        font_row.addWidget(self.font_box, 1)
        font_row.addWidget(self.font_size)
        font_row.addWidget(self.font_color)
        f.addRow("Default font:", font_row)
        self.sample = QLabel("The quick brown fox jumps over the lazy dog.")
        self.sample.setAutoFillBackground(True)
        pal = self.sample.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("white"))
        self.sample.setPalette(pal)
        self.sample.setContentsMargins(8, 8, 8, 8)
        f.addRow("", self.sample)
        self._show_color()
        for sig in (self.font_box.currentFontChanged, self.font_size.valueChanged):
            sig.connect(lambda *_: self._show_color())
        tabs.addTab(compose, "Compose")

        self._signatures: dict[str, tuple[str, bool, bool]] = {}
        self._sig_shown = None
        if store is not None and accounts:
            from .rich_editor import RichEditor
            sigs = QWidget()
            v = QVBoxLayout(sigs)
            top = QHBoxLayout()
            top.addWidget(QLabel("Account:"))
            self.sig_account = QComboBox()
            for account_id, address in accounts:
                self.sig_account.addItem(address, account_id)
                row = store.signature(account_id)
                self._signatures[account_id] = (row["html"], bool(row["on_new"]),
                                                bool(row["on_reply"])) if row else ("", True, True)
            top.addWidget(self.sig_account, 1)
            v.addLayout(top)
            self.sig_editor = RichEditor()
            self.sig_editor.set_default_font(settings.get("compose/font"),
                                             settings.get("compose/size"),
                                             settings.get("compose/color"))
            v.addWidget(self.sig_editor, 1)
            self.sig_new = QCheckBox("Add it to new messages")
            self.sig_reply = QCheckBox("Add it to replies and forwards")
            v.addWidget(self.sig_new)
            v.addWidget(self.sig_reply)
            hint = QLabel("In a message, “Insert signature” adds it just above the quoted text.")
            hint.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            v.addWidget(hint)
            self._sig_shown = None
            self.sig_account.currentIndexChanged.connect(lambda _: self._show_signature())
            self._show_signature()
            tabs.addTab(sigs, "Signatures")

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(box)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose data folder", self.path.text())
        if d:
            self.path.setText(d)

    # -- compose --------------------------------------------------------------------------
    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "Text colour")
        if color.isValid():
            self._color = color
            self._show_color()

    def _show_color(self):
        self.font_color.setText(self._color.name())
        self.font_color.setStyleSheet(f"color: {self._color.name()}")
        font = QFont(self.font_box.currentFont())
        font.setPointSize(self.font_size.value())
        self.sample.setFont(font)
        self.sample.setStyleSheet(f"color: {self._color.name()}; background: white")

    # -- signatures ---------------------------------------------------------------------------
    def _keep_signature(self):
        if self._sig_shown is not None:
            text = self.sig_editor.text().strip()
            html = self.sig_editor.html() if text or "<img" in self.sig_editor.html() else ""
            self._signatures[self._sig_shown] = (html, self.sig_new.isChecked(),
                                                 self.sig_reply.isChecked())

    def _show_signature(self):
        self._keep_signature()
        account_id = self.sig_account.currentData()
        html, on_new, on_reply = self._signatures.get(account_id, ("", True, True))
        if html:
            self.sig_editor.edit.setHtml(html)
        else:
            self.sig_editor.edit.clear()
        self.sig_new.setChecked(on_new)
        self.sig_reply.setChecked(on_reply)
        self._sig_shown = account_id

    def _save(self) -> None:
        s = self.s
        s.set("compose/html", self.html_format.isChecked())
        if self.font_box.currentFont().family() != self._shown_font:
            s.set("compose/font", self.font_box.currentFont().family())
        s.set("compose/size", self.font_size.value())
        s.set("compose/color", self._color.name())
        if self._signatures:
            self._keep_signature()
            for account_id, (html, on_new, on_reply) in self._signatures.items():
                self.store.set_signature(account_id, html, on_new, on_reply)
        s.set("images/load_remote", self.images.isChecked())
        s.set("sync/interval_min", self.interval.value())
        s.set("notify/new_mail", self.notify.isChecked())
        s.set("tray/show", self.tray.isChecked())
        s.set("tray/close_to_tray", self.close_to_tray.isChecked())
        s.set("tray/icon", self.tray_icon.currentData())
        s.set("view/splash", self.splash.isChecked())
        s.set("reminders/enabled", self.reminders.isChecked())
        s.set("sync/window_days", self.sync_window.days())
        s.set("list/window_days", self.list_window.days())
        s.set("read/mark_on_view", self.mark_read.isChecked())
        s.set("read/mark_delay_sec", self.mark_delay.value())
        s.set("storage/path", self.path.text().strip())
        s.set("cache/max_mb", self.cache_mb.value())
        s.set("cache/max_age_days", self.cache_age.days())
        self.accept()
