"""Launcher/icon install (into a temporary XDG data folder) and notification messages."""
from __future__ import annotations

import queue

import pytest

from waddlemail import desktop


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(desktop, "_refresh_caches", lambda: None)  # leave the real caches be
    monkeypatch.setenv("WADDLEMAIL_CLIENT_ID", "abc-123")
    return tmp_path


def test_install_and_uninstall(xdg):
    written = desktop.install()
    entry = (xdg / "applications" / "waddlemail.desktop").read_text()
    assert "Icon=waddlemail" in entry and "StartupWMClass=WaddleMail" in entry
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    assert "WADDLEMAIL_CLIENT_ID=abc-123" in exec_line and exec_line.endswith(" %u")
    assert "-m waddlemail" in exec_line or exec_line.split()[-2].endswith("waddlemail")
    for size in (16, 48, 512):
        assert (xdg / f"icons/hicolor/{size}x{size}/apps/waddlemail.png").exists()
    assert (xdg / "icons/hicolor/scalable/apps/waddlemail.svg").exists()
    assert (xdg / "icons/hicolor/symbolic/apps/waddlemail-unread-symbolic.svg").exists()
    assert desktop.is_installed()
    removed = desktop.uninstall()
    assert set(removed) == set(written) and not desktop.is_installed()


def test_exec_quoting():
    assert desktop._quote("/usr/bin/python3") == "/usr/bin/python3"
    assert desktop._quote("/home/a b/py") == '"/home/a b/py"'
    assert desktop._quote('x"$y') == '"x\\"\\$y"'


class FakeRouter:
    def __init__(self):
        self.sent = []

    def send_and_get_reply(self, msg, timeout=None):
        self.sent.append(msg)

        class Reply:
            body = (42,)
        return Reply()

    def send(self, msg):
        self.sent.append(msg)


def make_notifier(caps):
    from jeepney import DBusAddress, new_method_call
    from waddlemail.notify import FreedesktopNotifier
    n = FreedesktopNotifier.__new__(FreedesktopNotifier)  # no real D-Bus connection
    super(FreedesktopNotifier, n).__init__()
    n._new_method_call = new_method_call
    n._addr = DBusAddress("/org/freedesktop/Notifications",
                          bus_name="org.freedesktop.Notifications",
                          interface="org.freedesktop.Notifications")
    n._router, n.caps, n._keys, n._signals = FakeRouter(), set(caps), {}, queue.Queue()
    return n


def test_notify_message():
    from jeepney import HeaderFields
    n = make_notifier({"actions", "body-markup", "sound", "x-kde-origin-name"})
    n.notify("Priya", "Budget <final> & more", "F|m1", origin="me@x.com")
    msg = n._router.sent[0]
    assert msg.header.fields[HeaderFields.signature] == "susssasa{sv}i"
    app, replaces, icon, summary, body, actions, hints, timeout = msg.body
    assert (app, replaces, summary, actions, timeout) == ("WaddleMail", 0, "Priya",
                                                          ["default", "Open"], -1)
    assert body == "Budget &lt;final&gt; &amp; more" and icon.endswith("128.png")
    assert hints["desktop-entry"] == ("s", "waddlemail")
    assert hints["x-kde-origin-name"] == ("s", "me@x.com")
    assert n._keys == {42: "F|m1"}

    plain = make_notifier(set())  # a minimal server: no markup, no buttons
    plain.notify("S", "a < b", "k")
    assert plain._router.sent[0].body[4] == "a < b" and plain._router.sent[0].body[5] == []


def test_click_and_badge(monkeypatch):
    from jeepney import DBusAddress, HeaderFields, new_signal
    n = make_notifier({"actions"})
    n._keys[7] = "F|m9"
    got = []
    n.activated.connect(got.append)
    addr = DBusAddress("/org/freedesktop/Notifications",
                       interface="org.freedesktop.Notifications")
    n._signals.put(new_signal(addr, "ActivationToken", "us", (7, "tok")))
    n._signals.put(new_signal(addr, "ActionInvoked", "us", (7, "default")))
    n._signals.put(None)
    monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
    import os
    n._listen()
    assert got == ["F|m9"] and os.environ["XDG_ACTIVATION_TOKEN"] == "tok"
    n.set_badge(5)
    badge = n._router.sent[-1]
    assert badge.header.fields[HeaderFields.member] == "Update"
    assert badge.body == ("application://waddlemail.desktop",
                          {"count": ("x", 5), "count-visible": ("b", True)})
