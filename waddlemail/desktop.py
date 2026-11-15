"""Desktop integration files: the app icon, the tray icons, and (on Linux) a launcher in the
application menu.

The launcher and icons go to the user's XDG data folders (~/.local/share/applications and
~/.local/share/icons/hicolor), so nothing needs root. `waddlemail --install-desktop` /
`--uninstall-desktop`, or Help > Add to application menu, do the same.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

APP_ID = "waddlemail"  # desktop file name, icon name, Wayland app id
RESOURCES = Path(__file__).parent / "resources"
PNG_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)
TRAY_ICONS = ("waddlemail-symbolic", "waddlemail-unread-symbolic")


def icon_png(size: int = 128) -> Path:
    return RESOURCES / "png" / f"{size}.png"


def icon_svg() -> Path:
    return RESOURCES / "waddlemail.svg"


def tray_svg(unread: bool) -> Path:
    return RESOURCES / f"{TRAY_ICONS[1] if unread else TRAY_ICONS[0]}.svg"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_file() -> Path:
    return _data_home() / "applications" / f"{APP_ID}.desktop"


def _icon_targets() -> list[tuple[Path, Path]]:
    """(source, installed path) for every icon file."""
    hicolor = _data_home() / "icons" / "hicolor"
    out = [(icon_png(s), hicolor / f"{s}x{s}" / "apps" / f"{APP_ID}.png") for s in PNG_SIZES]
    out.append((icon_svg(), hicolor / "scalable" / "apps" / f"{APP_ID}.svg"))
    out += [(RESOURCES / f"{n}.svg", hicolor / "symbolic" / "apps" / f"{n}.svg")
            for n in TRAY_ICONS]
    return out


def is_installed() -> bool:
    return desktop_file().exists()


def _quote(arg: str) -> str:
    """Quote one Exec argument as the Desktop Entry spec asks."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def launch_command() -> list[str]:
    """How the menu entry starts this WaddleMail: the installed `waddlemail` script if there
    is one next to this Python, else `python -m waddlemail` (from the source tree if need be).
    The Entra client id (and tenant) in use now are passed along, since a menu launch has no
    shell environment."""
    env = [f"{k}={os.environ[k]}" for k in ("WADDLEMAIL_CLIENT_ID", "WADDLEMAIL_TENANT")
           if os.environ.get(k)]
    script = Path(sys.executable).with_name("waddlemail")
    if script.exists():
        cmd = [str(script)]
    else:
        cmd = [sys.executable, "-m", "waddlemail"]
        source_root = Path(__file__).resolve().parent.parent
        installed = {Path(p).resolve() for p in (sysconfig.get_paths()["purelib"],
                                                 sysconfig.get_paths()["platlib"])}
        if source_root not in installed:  # running from a checkout
            env.append(f"PYTHONPATH={source_root}")
    return (["env", *env] if env else []) + cmd


def install() -> list[Path]:
    """Install the launcher and icons for this user. Returns the files written."""
    written = []
    for src, dst in _icon_targets():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        written.append(dst)
    cmd = launch_command()
    text = (RESOURCES / "waddlemail.desktop").read_text()
    text = text.replace("@EXEC@", " ".join(_quote(a) for a in cmd))
    program = next(a for a in cmd if a != "env" and "=" not in a)
    text = text.replace("@TRYEXEC@", program)  # menus hide the entry if this disappears
    target = desktop_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    written.append(target)
    _refresh_caches()
    return written


def uninstall() -> list[Path]:
    removed = []
    for _, dst in _icon_targets():
        if dst.exists():
            dst.unlink()
            removed.append(dst)
    if desktop_file().exists():
        desktop_file().unlink()
        removed.append(desktop_file())
    _refresh_caches()
    return removed


def _refresh_caches() -> None:
    """Tell menus and icon themes about the change (each tool is optional)."""
    hicolor = _data_home() / "icons" / "hicolor"
    for cmd in (["gtk-update-icon-cache", "-f", "-t", "-q", str(hicolor)],
                ["update-desktop-database", "-q", str(desktop_file().parent)],
                ["kbuildsycoca6"], ["kbuildsycoca5"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=60)


def set_windows_app_id() -> None:
    """On Windows, give the process its own taskbar identity, so the taskbar shows the
    WaddleMail icon rather than Python's."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WaddleMail.WaddleMail")
    except Exception:  # cosmetic only
        pass

