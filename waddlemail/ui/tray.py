"""The system tray icon: the WaddleMail penguin in the panel's own colour, with a red dot
while the Inbox has unread mail. Click it to show or hide the window."""
from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..desktop import TRAY_ICONS, tray_svg
from . import iconset


def _icon(unread: bool) -> QIcon:
    # Installed in the icon theme (Help > Add to application menu), the panel recolours it to
    # match itself; otherwise tint our copy to the text colour.
    name = TRAY_ICONS[1] if unread else TRAY_ICONS[0]
    return QIcon.fromTheme(name, iconset.load_svg_file(tray_svg(unread)))


class Tray(QSystemTrayIcon):
    def __init__(self, window, check_mail, quit_app):
        super().__init__(_icon(False), window)
        self._window = window
        self._unread = -1
        menu = QMenu()
        self.act_show = QAction("&Open WaddleMail", menu)
        self.act_show.triggered.connect(self.show_window)
        act_check = QAction("&Check for new mail", menu)
        act_check.triggered.connect(lambda: check_mail())
        act_quit = QAction("&Quit", menu)
        act_quit.triggered.connect(lambda: quit_app())
        menu.addAction(self.act_show)
        menu.addAction(act_check)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._menu = menu  # keep it alive
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self.set_unread(0)

    def set_unread(self, count: int) -> None:
        if count == self._unread:
            return
        if (count > 0) != (self._unread > 0):
            self.setIcon(_icon(count > 0))
        self._unread = count
        self.setToolTip("WaddleMail" + (f" – {count} unread" if count else ""))

    def show_window(self):
        w = self._window
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def _on_activated(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:  # left click only
            return
        w = self._window
        if w.isVisible() and w.isActiveWindow():  # in front: put it away
            if w.close_to_tray():
                w.hide()
            else:
                w.showMinimized()
        else:
            self.show_window()
