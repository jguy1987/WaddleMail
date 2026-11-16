#!/bin/sh
# Install the desktop file and icons under $1 (a buildroot / debian staging dir), prefix /usr.
set -e
root="$1"
res=waddlemail/resources
share="$root/usr/share"
install -d "$share/applications"
sed -e 's|@EXEC@|waddlemail|' -e 's|@TRYEXEC@|waddlemail|' \
    "$res/waddlemail.desktop" > "$share/applications/waddlemail.desktop"
chmod 644 "$share/applications/waddlemail.desktop"
for s in 16 22 24 32 48 64 128 256 512; do
    install -Dm644 "$res/png/$s.png" "$share/icons/hicolor/${s}x${s}/apps/waddlemail.png"
done
install -Dm644 "$res/waddlemail.svg" "$share/icons/hicolor/scalable/apps/waddlemail.svg"
for n in waddlemail-symbolic waddlemail-unread-symbolic; do
    install -Dm644 "$res/$n.svg" "$share/icons/hicolor/symbolic/apps/$n.svg"
done
