"""User preferences, persisted with QSettings (~/.config/WaddleMail/WaddleMail.conf)."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

DEFAULTS = {
    "images/load_remote": False,   # load remote images by default
    "storage/path": "",            # data folder; empty = default location
    "sync/window_days": 90,        # how much mail to keep locally; 0 = all
    "list/window_days": 0,         # how much to show in the message list; 0 = all
    "cache/max_mb": 200,           # message body cache size limit
    "cache/max_age_days": 0,       # discard cached bodies not opened for this long; 0 = never
    "sync/interval_min": 1,        # check for new mail every N minutes
    "read/mark_on_view": True,     # mark messages read when shown in the reading pane...
    "read/mark_delay_sec": 0,      # ...after this many seconds
    "view/reading_pane": "bottom", # right / bottom / off
    "view/filter_bar": True,       # quick filter bar above the message list
    "list/threads": True,          # group messages into conversations
    "list/date_groups": True,      # Today / Yesterday / ... headings when sorted by date
    "search/server": True,         # quick filter also searches the server
    "notify/new_mail": True,       # desktop notification when mail arrives in an Inbox
    "tray/show": True,             # icon in the system tray / status area
    "tray/close_to_tray": False,   # closing the window keeps WaddleMail running in the tray
    "view/splash": True,           # splash screen while starting
    "reminders/enabled": True,     # event reminders (window + notification)
    "compose/html": True,          # write new mail in HTML (else plain text)
    "compose/font": "Calibri",     # default font for new mail...
    "compose/size": 11,            # ...its size in points...
    "compose/color": "#000000",    # ...and colour
    # Window layout, saved on exit (not shown in Preferences).
    "window/geometry": QByteArray(),
    "window/folder_split": QByteArray(),
    "window/favorites_split": QByteArray(),
    "window/split_right": QByteArray(),
    "window/split_bottom": QByteArray(),
    "list/header": QByteArray(),   # column order, widths, visibility and sort
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

    def download_days(self) -> int:
        """How far back mail is downloaded (headers only; 0 = all): the longer of "Keep mail
        locally" and "Show in message list", since the list can only show downloaded mail."""
        keep, show = self.get("sync/window_days"), self.get("list/window_days")
        return 0 if keep <= 0 or show <= 0 else max(keep, show)

    def set(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self._q.setValue(key, value)
        self._q.sync()
