from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("WaddleMail")

    from .auth import AuthError, Authenticator
    from .mailboxes import MailboxManager
    from .settings import Settings
    from .store import Store
    from .ui.main_window import MainWindow

    try:
        auth = Authenticator()
    except AuthError as exc:
        QMessageBox.critical(None, "WaddleMail", str(exc))
        return 1

    settings = Settings()
    data = Path(settings.get("storage/path") or config.data_dir())
    data.mkdir(parents=True, exist_ok=True)

    store = Store(data / "mail.db")
    window = MainWindow(store, MailboxManager(store, auth), settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
