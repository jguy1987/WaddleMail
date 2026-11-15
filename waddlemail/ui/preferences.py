"""Preferences dialog."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLineEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..settings import Settings


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
    def __init__(self, settings: Settings, clear_cache, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Preferences")
        self.resize(560, 360)
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
        tabs.addTab(general, "General")

        mail = QWidget()
        f = QFormLayout(mail)
        self.sync_window, self.list_window = PeriodEdit(), PeriodEdit()
        self.sync_window.set_days(settings.get("sync/window_days"))
        self.list_window.set_days(settings.get("list/window_days"))
        f.addRow("Keep mail locally for:", self.sync_window)
        f.addRow("Show in message list:", self.list_window)
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

    def _save(self) -> None:
        s = self.s
        s.set("images/load_remote", self.images.isChecked())
        s.set("sync/interval_min", self.interval.value())
        s.set("sync/window_days", self.sync_window.days())
        s.set("list/window_days", self.list_window.days())
        s.set("read/mark_on_view", self.mark_read.isChecked())
        s.set("read/mark_delay_sec", self.mark_delay.value())
        s.set("storage/path", self.path.text().strip())
        s.set("cache/max_mb", self.cache_mb.value())
        s.set("cache/max_age_days", self.cache_age.days())
        self.accept()
