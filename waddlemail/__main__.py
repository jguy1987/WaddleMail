from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import config

USAGE = """usage: waddlemail [--install-desktop | --uninstall-desktop]

  --install-desktop     add WaddleMail and its icon to your application menu (this user)
  --uninstall-desktop   remove them again
"""


def _desktop_command(arg: str) -> int:
    from . import desktop
    if not sys.platform.startswith("linux"):
        print("Application menu entries are only made on Linux.", file=sys.stderr)
        return 1
    files = desktop.install() if arg == "--install-desktop" else desktop.uninstall()
    for f in files:
        print(f)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("--install-desktop", "--uninstall-desktop"):
        return _desktop_command(args[0])
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    from PySide6.QtCore import QSize
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import desktop
    desktop.set_windows_app_id()  # before any window exists

    app = QApplication(sys.argv)
    app.setApplicationName("WaddleMail")
    # Wayland app id / X11 class: lets the desktop match windows to waddlemail.desktop.
    app.setDesktopFileName(desktop.APP_ID)
    icon = QIcon()
    for size in desktop.PNG_SIZES:
        icon.addFile(str(desktop.icon_png(size)), QSize(size, size))
    app.setWindowIcon(icon)

    from .settings import Settings
    settings = Settings()
    splash = None
    if settings.get("view/splash"):
        from .ui.splash import Splash
        splash = Splash()
        splash.show()
        splash.status("Starting…")

    from .auth import AuthError, Authenticator

    try:
        auth = Authenticator()
    except AuthError as exc:
        if splash:
            splash.close()
        QMessageBox.critical(None, "WaddleMail", str(exc))
        return 1

    if splash:
        splash.status("Opening your mail…")
    from .mailboxes import MailboxManager
    from .store import Store

    data = Path(settings.get("storage/path") or config.data_dir())
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data / "mail.db")

    if splash:
        splash.status("Loading…")
    from .ui.main_window import MainWindow  # the web engine makes this the slow part

    window = MainWindow(store, MailboxManager(store, auth), settings)
    window.show()
    if splash:
        splash.finish(window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
