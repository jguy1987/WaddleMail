"""The tray icon follows the theme: light on a dark theme, dark on a light one."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from waddlemail.ui import tray as tray_mod  # noqa: E402
from waddlemail.ui.tray import DARK, LIGHT, Tray, _icon, icon_color  # noqa: E402


def ink(icon, size=48) -> QColor:
    """The colour the icon is mostly drawn in (the unread dot's red is far less of it)."""
    image = icon.pixmap(size, size).toImage()
    seen: dict[str, int] = {}
    for x in range(image.width()):
        for y in range(image.height()):
            c = image.pixelColor(x, y)
            if c.alpha() > 200:
                seen[c.name()] = seen.get(c.name(), 0) + 1
    return QColor(max(seen, key=seen.get))


def close(a: QColor, b: QColor) -> bool:
    """(anti-aliasing moves a channel by a unit or two)"""
    return all(abs(x - y) <= 3 for x, y in ((a.red(), b.red()), (a.green(), b.green()),
                                            (a.blue(), b.blue())))


def test_the_icon_is_light_on_a_dark_theme_and_dark_on_a_light_one(qapp, monkeypatch):
    monkeypatch.setattr(tray_mod, "dark_theme", lambda: True)
    assert icon_color("auto") == LIGHT
    assert close(ink(_icon(False, "auto")), LIGHT)
    monkeypatch.setattr(tray_mod, "dark_theme", lambda: False)
    assert icon_color("auto") == DARK
    assert close(ink(_icon(False, "auto")), DARK)


def test_light_and_dark_can_be_forced_and_system_is_left_to_the_desktop(qapp, monkeypatch):
    monkeypatch.setattr(tray_mod, "dark_theme", lambda: True)
    assert icon_color("dark") == DARK and icon_color("light") == LIGHT
    assert icon_color("system") is None
    assert close(ink(_icon(False, "dark")), DARK)   # whatever the theme says
    assert not _icon(False, "system").isNull()


def test_the_unread_icon_keeps_its_red_dot_in_either_colour(qapp):
    for mode in ("light", "dark"):
        image = _icon(True, mode).pixmap(48, 48).toImage()
        reds = [image.pixelColor(x, y) for x in range(48) for y in range(48)
                if image.pixelColor(x, y).alpha() > 200
                and image.pixelColor(x, y).red() > 200 and image.pixelColor(x, y).green() < 120]
        assert len(reds) > 20, mode


def test_the_tray_redraws_when_the_theme_or_the_setting_changes(qapp, monkeypatch):
    theme = {"dark": True}
    monkeypatch.setattr(tray_mod, "dark_theme", lambda: theme["dark"])
    window = QWidget()
    tray = Tray(window, lambda: None, lambda: None)
    tray.set_mode("auto")
    assert close(ink(tray.icon()), LIGHT)
    theme["dark"] = False
    tray._redraw()                       # (what a theme switch calls)
    assert close(ink(tray.icon()), DARK)
    tray.set_mode("light")
    assert close(ink(tray.icon()), LIGHT)
    tray.set_unread(3)                   # the unread icon is drawn in the chosen colour too
    assert close(ink(tray.icon()), LIGHT)
    window.deleteLater()
    tray.deleteLater()


def test_a_palette_change_redraws_the_icon_without_an_application_wide_filter(qapp, monkeypatch):
    """(An application-wide Python event filter crashed the app when a message was opened.)"""
    from PySide6.QtGui import QPalette

    theme = {"dark": True}
    monkeypatch.setattr(tray_mod, "dark_theme", lambda: theme["dark"])
    window = QWidget()
    window.show()
    tray = Tray(window, lambda: None, lambda: None)
    assert close(ink(tray.icon()), LIGHT)
    theme["dark"] = False
    old = qapp.palette()
    palette = QPalette(old)
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    qapp.setPalette(palette)                     # what a theme switch does
    qapp.processEvents()
    try:
        assert close(ink(tray.icon()), DARK)
    finally:
        qapp.setPalette(old)
        window.deleteLater()
        tray.deleteLater()
