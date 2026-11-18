"""The system tray icon: the WaddleMail penguin in the panel's own colour, with a red dot
while the Inbox has unread mail. Click it to show or hide the window."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..desktop import TRAY_ICONS, tray_svg
from . import iconset

# What Preferences offers, and what each does:
#   auto    a light icon on a dark theme, a dark one on a light theme
#   light / dark   always that, for a panel whose colour doesn't follow the theme
#   system  hand the desktop an icon name and let it recolour (what older versions did)
MODES = (("auto", "Match the theme"), ("light", "Light icon (for a dark panel)"),
         ("dark", "Dark icon (for a light panel)"), ("system", "Let the desktop choose"))
LIGHT, DARK = QColor("#fcfcfc"), QColor("#232629")  # the icon's colour (Breeze's text colours)


def dark_theme() -> bool:
    """Whether the desktop's theme is dark: what it says, else how dark the windows are."""
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return scheme == Qt.ColorScheme.Dark
    return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def icon_color(mode: str) -> QColor | None:
    """The colour to draw the icon in; None to leave it to the desktop."""
    if mode == "system":
        return None
    if mode == "light" or (mode != "dark" and dark_theme()):
        return LIGHT
    return DARK


def _icon(unread: bool, mode: str = "auto") -> QIcon:
    color = icon_color(mode)
    own = iconset.load_svg_file(tray_svg(unread), color)  # (drawn here, in our colour)
    if color is not None:
        return own
    # Installed in the icon theme (Help > Add to application menu) the panel may recolour it
    # to match itself; if the theme doesn't have it, our own copy.
    return QIcon.fromTheme(TRAY_ICONS[1] if unread else TRAY_ICONS[0], own)


class _ThemeWatcher(QObject):
    """Calls `changed` when the window is told the application's palette changed (a theme
    switch). It watches that one window only: a filter on the whole application sees every
    event of every object, WebEngine's half-built ones included, and crashes PySide."""

    def __init__(self, window, changed):
        super().__init__(window)
        self._changed = changed
        window.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ApplicationPaletteChange:
            self._changed()
        return False


class Tray(QSystemTrayIcon):
    def __init__(self, window, check_mail, quit_app):
        super().__init__(_icon(False), window)
        self._window = window
        self._unread = -1
        self._mode = "auto"
        # a theme switch while running: draw the icon again in the right colour
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self._redraw())
        self._watcher = _ThemeWatcher(window, self._redraw)
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

    def set_mode(self, mode: str) -> None:
        """The icon's colour (one of MODES' keys)."""
        if mode != self._mode:
            self._mode = mode
            self._redraw()

    def _redraw(self) -> None:
        self.setIcon(_icon(self._unread > 0, self._mode))

    def set_unread(self, count: int) -> None:
        if count == self._unread:
            return
        if (count > 0) != (self._unread > 0):
            self.setIcon(_icon(count > 0, self._mode))
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
