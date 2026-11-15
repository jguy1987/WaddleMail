"""User preferences, persisted with QSettings (~/.config/WaddleMail/WaddleMail.conf)."""
from __future__ import annotations

from PySide6.QtCore import QSettings

DEFAULTS = {
    "images/load_remote": False,   # load remote images by default
    "storage/path": "",            # data folder; empty = default location
    "sync/window_days": 90,        # how much mail to keep locally; 0 = all
    "list/window_days": 0,         # how much to show in the message list; 0 = all
    "cache/max_mb": 200,           # message body cache size limit
    "cache/max_age_days": 0,       # discard cached bodies not opened for this long; 0 = never
    "sync/interval_min": 1,        # check for new mail every N minutes
}


class Settings:
    def __init__(self, path: str | None = None) -> None:
        # An explicit INI path is used by the tests; the app uses the default location.
        if path:
            self._q = QSettings(path, QSettings.Format.IniFormat)
        else:
            self._q = QSettings("WaddleMail", "WaddleMail")

    def get(self, key: str):
        default = DEFAULTS[key]
        return self._q.value(key, default, type=type(default))

    def set(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self._q.setValue(key, value)
        self._q.sync()
