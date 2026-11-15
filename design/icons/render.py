import sys
from pathlib import Path
from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

app = QGuiApplication(sys.argv)
ROOT = Path(__file__).parent
SVG = ROOT / "svg"

def render(path, size, color=None):
    text = path.read_text()
    if color:
        text = text.replace("currentColor", color)
    r = QSvgRenderer(QByteArray(text.encode()))
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing); r.render(p); p.end()
    return img

concepts = [("concept-a-courier", "A  Courier"), ("concept-b-peek", "B  Peek"), ("concept-c-badge", "C  Badge")]
sizes = [128, 64, 48, 32, 24, 16]
for name, _ in concepts:
    out = ROOT / "png" / name
    out.mkdir(parents=True, exist_ok=True)
    for s in (16, 22, 24, 32, 48, 64, 128, 256, 512):
        render(SVG / f"{name}.svg", s).save(str(out / f"{s}.png"))
sym = ROOT / "png" / "tray-symbolic"
sym.mkdir(parents=True, exist_ok=True)
for name in ("tray-symbolic", "tray-symbolic-unread"):
    for s in (16, 22, 24, 32, 48):
        render(SVG / f"{name}.svg", s, "#2b2b2e").save(str(sym / f"{name}-{s}-dark-on-light.png"))
        render(SVG / f"{name}.svg", s, "#f2f2f2").save(str(sym / f"{name}-{s}-light-on-dark.png"))

# contact sheet
W, H = 1180, 700
sheet = QImage(W, H, QImage.Format.Format_ARGB32); sheet.fill(QColor("#ffffff"))
p = QPainter(sheet); p.setRenderHint(QPainter.RenderHint.Antialiasing)
f = QFont(); f.setPointSize(11); p.setFont(f)
panels = [(0, "#f4f4f6", "#2b2b2e"), (590, "#2b2b2e", "#f2f2f2")]
for x0, bg, fg in panels:
    p.fillRect(x0, 0, 590, H, QColor(bg))
    p.setPen(QColor(fg))
    p.drawText(x0 + 20, 28, "light panel" if bg == "#f4f4f6" else "dark panel")
    for row, (name, label) in enumerate(concepts):
        y = 50 + row * 175
        p.drawText(x0 + 20, y + 14, label)
        x = x0 + 20
        for s in sizes:
            p.drawImage(x, y + 24, render(SVG / f"{name}.svg", s))
            x += s + 18
    y = 50 + 3 * 175 + 10
    p.drawText(x0 + 20, y, "Tray, symbolic: plain / unread")
    x = x0 + 20
    for name in ("tray-symbolic", "tray-symbolic-unread"):
        for s in (48, 32, 24, 16):
            p.drawImage(x, y + 14, render(SVG / f"{name}.svg", s, fg)); x += s + 16
        x += 24
p.end()
sheet.save(str(ROOT / "contact-sheet.png"))
print("done")
