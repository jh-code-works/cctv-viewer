"""CCTV 아이콘 생성: assets/cctv.ico (16~256 멀티 사이즈) + assets/cctv.png

    python assets/make_icon.py

작은 크기는 축소하면 뭉개지므로 사이즈마다 QPainter 로 직접 그린다.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QBuffer, QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QRadialGradient

HERE = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 256, size / 256)  # 256 기준 좌표로 그림
    small = size <= 24

    # 배경: 남색 라운드 사각형
    bg = QLinearGradient(0, 0, 0, 256)
    bg.setColorAt(0, QColor("#2b5c8a"))
    bg.setColorAt(1, QColor("#10243d"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 52, 52)

    # 벽 브래킷 + 암
    metal = QColor("#c9d3de")
    p.setBrush(metal)
    p.drawRoundedRect(QRectF(186, 150, 30, 64), 8, 8)
    p.setPen(QPen(metal, 18 if not small else 26, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(200, 170), QPointF(160, 132))
    p.setPen(Qt.PenStyle.NoPen)

    # 카메라 본체 (불릿형, 좌측 아래를 향해 기울어짐)
    p.save()
    p.translate(140, 110)
    p.rotate(18)
    body = QLinearGradient(0, -40, 0, 40)
    body.setColorAt(0, QColor("#ffffff"))
    body.setColorAt(1, QColor("#b8c4d0"))
    p.setBrush(body)
    p.drawRoundedRect(QRectF(-92, -34, 160, 68), 22, 22)
    # 선쉐이드
    p.setBrush(QColor("#e8eef4"))
    p.drawRoundedRect(QRectF(-104, -46, 176, 22), 10, 10)
    # 렌즈 링
    p.setBrush(QColor("#1b2a3a"))
    p.drawEllipse(QPointF(-92, 0), 30, 30)
    lens = QRadialGradient(QPointF(-98, -6), 24)
    lens.setColorAt(0, QColor("#6fd0ff"))
    lens.setColorAt(0.45, QColor("#1a6fb0"))
    lens.setColorAt(1, QColor("#08121d"))
    p.setBrush(lens)
    p.drawEllipse(QPointF(-92, 0), 20, 20)
    if not small:
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawEllipse(QPointF(-99, -7), 5, 5)
    p.restore()

    # REC 표시등
    p.setBrush(QColor("#ff3b30"))
    p.setPen(QPen(QColor("#ffffff"), 5) if not small else Qt.PenStyle.NoPen)
    r = 22 if not small else 30
    p.drawEllipse(QPointF(58, 196), r, r)
    p.end()
    return img


def to_pil(img: QImage) -> Image.Image:
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 (QPainter 텍스트/폰트용)
    images = [to_pil(render(s)) for s in SIZES]
    images[-1].save(HERE / "cctv.png")
    images[-1].save(HERE / "cctv.ico", format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print(f"생성: {HERE / 'cctv.ico'}  ({', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
