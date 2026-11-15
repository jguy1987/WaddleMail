WaddleMail icon samples (original artwork, made for this project)

contact-sheet.png   all concepts at several sizes on light and dark panels
svg/                sources
  concept-a-courier.svg   penguin holding an envelope
  concept-b-peek.svg      penguin peeking out of an open envelope
  concept-c-badge.svg     round badge (reads well tiny, good for a tray icon)
  tray-symbolic.svg / tray-symbolic-unread.svg   one-colour 24px outlines for the tray
png/                16-512 px renders of each concept; tray PNGs in dark and light variants
render.py           regenerates png/ and contact-sheet.png (needs PySide6)
waddlemail.desktop  sample launcher entry for the program menu

Trying one out (once you pick):
  ~/.local/share/icons/hicolor/scalable/apps/waddlemail.svg        <- the chosen concept SVG
  ~/.local/share/icons/hicolor/scalable/apps/waddlemail-symbolic.svg   <- tray-symbolic.svg
  ~/.local/share/applications/waddlemail.desktop
  then run: gtk-update-icon-cache -f ~/.local/share/icons/hicolor   (if available)

In the app: QApplication.setWindowIcon(QIcon("path/to/svg")) for the window/taskbar, and
QSystemTrayIcon(QIcon(...)) for the tray. KDE and GNOME recolour *-symbolic icons to match the
panel; GNOME also needs the AppIndicator extension to show tray icons at all.

Licence: these are original designs; use them for WaddleMail however you like.

Chosen: concept A (Courier) is the app icon, copied to waddlemail/resources/ (waddlemail.svg and
png/); the tray icons there are tray-symbolic*.svg with a Plasma colour-scheme class added.
