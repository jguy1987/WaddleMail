"""Desktop notifications for new mail, and the unread count on the task manager icon.

On Linux this talks to the freedesktop notification service (KDE, GNOME, XFCE, ...) over
D-Bus with jeepney, which gives click-to-open, the app's icon and name, a sound, and (on KDE)
the mailbox name. Clicking a notification emits `activated` with the key it was sent with.
The unread count goes to the Unity LauncherEntry interface, which KDE's task manager (and
docks such as Dash to Dock) show as a badge.

Elsewhere (Windows, macOS) the tray icon's own notifications are used.
"""
from __future__ import annotations

import html
import logging
import os
import queue
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

from .desktop import APP_ID, icon_png

log = logging.getLogger(__name__)

_PATH = "/org/freedesktop/Notifications"
_NAME = "org.freedesktop.Notifications"


class Notifier(QObject):
    """No-op base: used when nothing better is available."""
    activated = Signal(str)  # the key of the notification that was clicked

    def notify(self, summary: str, body: str, key: str, origin: str = "") -> None:
        pass

    def set_badge(self, count: int) -> None:
        pass

    def close(self) -> None:
        pass


class FreedesktopNotifier(Notifier):
    def __init__(self):
        super().__init__()
        from jeepney import DBusAddress, MatchRule, new_method_call
        from jeepney.bus_messages import message_bus
        from jeepney.io.threading import open_dbus_router

        self._new_method_call = new_method_call
        self._addr = DBusAddress(_PATH, bus_name=_NAME, interface=_NAME)
        self._router = open_dbus_router(bus="SESSION")
        try:
            reply = self._router.send_and_get_reply(
                new_method_call(self._addr, "GetCapabilities"), timeout=5)
            self.caps = set(reply.body[0])
            rule = MatchRule(type="signal", interface=_NAME, path=_PATH)
            self._router.send_and_get_reply(message_bus.AddMatch(rule), timeout=5)
        except Exception:
            self._router.close()
            raise
        self._signals: queue.Queue = queue.Queue()
        self._filter = self._router.filter(rule, queue=self._signals)
        self._keys: dict[int, str] = {}  # notification id -> key
        threading.Thread(target=self._listen, name="notifications", daemon=True).start()

    def notify(self, summary: str, body: str, key: str, origin: str = "") -> None:
        if "body-markup" in self.caps:  # then &, < and > must be escaped
            body = html.escape(body, quote=False)
        hints = {"desktop-entry": ("s", APP_ID), "category": ("s", "email.arrived")}
        if "sound" in self.caps:
            hints["sound-name"] = ("s", "message-new-email")
        if origin and "x-kde-origin-name" in self.caps:
            hints["x-kde-origin-name"] = ("s", origin)
        actions = ["default", "Open"] if "actions" in self.caps else []
        msg = self._new_method_call(
            self._addr, "Notify", "susssasa{sv}i",
            ("WaddleMail", 0, str(icon_png(128)), summary, body, actions, hints, -1))
        try:
            reply = self._router.send_and_get_reply(msg, timeout=5)
            self._keys[reply.body[0]] = key
        except Exception as exc:  # never let a notification break the app
            log.warning("notification failed: %s", exc)

    def set_badge(self, count: int) -> None:
        from jeepney import DBusAddress, new_signal
        addr = DBusAddress(f"/com/canonical/unity/launcherentry/{APP_ID}",
                           interface="com.canonical.Unity.LauncherEntry")
        props = {"count": ("x", count), "count-visible": ("b", count > 0)}
        try:
            self._router.send(new_signal(addr, "Update", "sa{sv}",
                                         (f"application://{APP_ID}.desktop", props)))
        except Exception as exc:
            log.debug("badge update failed: %s", exc)

    def _listen(self) -> None:
        from jeepney import HeaderFields
        while True:
            msg = self._signals.get()
            if msg is None:
                return
            member = msg.header.fields.get(HeaderFields.member)
            if member == "ActivationToken":
                # Sent just before ActionInvoked: lets Wayland bring our window to the front.
                os.environ["XDG_ACTIVATION_TOKEN"] = msg.body[1]
            elif member == "ActionInvoked":
                key = self._keys.get(msg.body[0])
                if key is not None:
                    self.activated.emit(key)  # delivered on the UI thread
            elif member == "NotificationClosed":
                self._keys.pop(msg.body[0], None)

    def close(self) -> None:
        self._signals.put(None)
        try:
            self._filter.close()
            self._router.close()
        except Exception:
            pass


class TrayNotifier(Notifier):
    """The tray icon's balloon / toast (Windows, macOS, Linux without the service)."""

    def __init__(self, tray: QSystemTrayIcon):
        super().__init__()
        self._tray = tray
        self._last = ""
        tray.messageClicked.connect(lambda: self.activated.emit(self._last))

    def notify(self, summary: str, body: str, key: str, origin: str = "") -> None:
        if self._tray.isVisible():
            self._last = key
            self._tray.showMessage(summary, body, QIcon(str(icon_png(64))), 10_000)


def make_notifier(tray: QSystemTrayIcon) -> Notifier:
    if sys.platform.startswith("linux") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        try:
            return FreedesktopNotifier()
        except Exception as exc:
            log.info("no notification service (%s); using the tray instead", exc)
    if QSystemTrayIcon.supportsMessages():
        return TrayNotifier(tray)
    return Notifier()
