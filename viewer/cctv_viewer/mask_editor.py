"""감지 제외 영역 편집 창: 카메라 화면 위에 펜으로 칠한다."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QHBoxLayout, QLabel, QPushButton, QRadioButton, QSlider, QVBoxLayout, QWidget,
)

from . import mask as M


class _Canvas(QWidget):
    def __init__(self, image: QImage | None, mask: np.ndarray) -> None:
        super().__init__()
        self.image = image
        self.mask = mask
        self.brush = 4       # 격자 칸 단위 반지름
        self.erase = False
        self._last: tuple[int, int] | None = None
        self._hover: QPointF | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _rect(self) -> QRectF:
        """영상이 그려지는 영역 (16:9 레터박스)."""
        w, h = self.width(), self.height()
        ar = (self.image.width() / self.image.height()) if self.image and self.image.height() else 16 / 9
        if w / h > ar:
            return QRectF((w - h * ar) / 2, 0, h * ar, h)
        return QRectF(0, (h - w / ar) / 2, w, w / ar)

    def _cell(self, pos: QPointF) -> tuple[int, int] | None:
        r = self._rect()
        if not r.contains(pos):
            return None
        return (int((pos.x() - r.left()) / r.width() * M.W), int((pos.y() - r.top()) / r.height() * M.H))

    def _stamp(self, cx: int, cy: int, value: bool) -> None:
        yy, xx = np.ogrid[:M.H, :M.W]
        self.mask[(xx - cx) ** 2 + (yy - cy) ** 2 <= self.brush ** 2] = value

    def _paint_to(self, cell: tuple[int, int], value: bool) -> None:
        # 빠르게 그어도 끊기지 않게 직전 점과 선으로 연결
        if self._last is None:
            self._stamp(*cell, value)
        else:
            (x0, y0), (x1, y1) = self._last, cell
            n = max(abs(x1 - x0), abs(y1 - y0), 1)
            for i in range(n + 1):
                self._stamp(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n), value)
        self._last = cell
        self.update()

    def mousePressEvent(self, e) -> None:
        self._last = None
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e) -> None:
        self._hover = e.position()
        buttons = e.buttons()
        if buttons & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            cell = self._cell(e.position())
            if cell:
                erase = self.erase or bool(buttons & Qt.MouseButton.RightButton)  # 오른쪽 버튼 = 지우개
                self._paint_to(cell, not erase)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        self._last = None

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 20, 20))
        r = self._rect()
        if self.image is not None:
            p.drawImage(r, self.image)
        else:
            p.setPen(QColor(150, 150, 150))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "화면을 불러오지 못했습니다 (연결 후 다시 열면 영상 위에 그릴 수 있습니다)")
        # 마스크: 반투명 빨강
        rgba = np.zeros((M.H, M.W, 4), np.uint8)
        rgba[self.mask] = (230, 40, 40, 120)
        overlay = QImage(rgba.data, M.W, M.H, M.W * 4, QImage.Format.Format_RGBA8888).copy()
        p.drawImage(r, overlay)  # 저해상도 격자를 그대로 늘려 표시 (칸 경계가 실제 판정 단위)
        # 붓 크기 미리보기
        if self._hover is not None and r.contains(self._hover):
            rad = self.brush * r.width() / M.W
            p.setPen(QPen(QColor(255, 255, 255, 200), 1, Qt.PenStyle.DashLine))
            p.drawEllipse(self._hover, rad, rad)
        p.end()


class MaskEditor(QDialog):
    """저장하면 self.mask (numpy bool 72x128) 에 결과가 남는다."""

    def __init__(self, cam_name: str, image: QImage | None, mask: np.ndarray | None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"감지 제외 영역 — {cam_name}")
        self.resize(1150, 760)
        self.canvas = _Canvas(image, (mask.copy() if mask is not None else M.empty()))

        pen = QRadioButton("✏ 펜 (칠하기)")
        eraser = QRadioButton("⌫ 지우개")
        pen.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(pen)
        group.addButton(eraser)
        eraser.toggled.connect(lambda on: setattr(self.canvas, "erase", on))
        size = QSlider(Qt.Orientation.Horizontal)
        size.setRange(1, 15)
        size.setValue(self.canvas.brush)
        size.setFixedWidth(160)
        size.valueChanged.connect(lambda v: setattr(self.canvas, "brush", v))
        clear = QPushButton("전체 지우기")
        clear.clicked.connect(self._clear)
        tools = QHBoxLayout()
        for w in (pen, eraser, QLabel("붓 크기"), size, clear):
            tools.addWidget(w)
        tools.addStretch(1)

        help_ = QLabel("빨갛게 칠한 곳에 서 있는 물체(박스의 발 위치 기준)는 AI 감지에서 제외됩니다. "
                       "오두막·천막·드럼통처럼 잘못 인식되는 곳이나 흔들리는 나무를 칠하세요. "
                       "사람이 지나다니는 바닥은 칠하지 마세요. (왼쪽 버튼: 칠하기, 오른쪽 버튼: 지우기)")
        help_.setWordWrap(True)
        help_.setStyleSheet("color: gray")

        save = QPushButton("저장")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(save)
        buttons.addWidget(cancel)

        lay = QVBoxLayout(self)
        lay.addLayout(tools)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(help_)
        lay.addLayout(buttons)

    def _clear(self) -> None:
        self.canvas.mask[:] = False
        self.canvas.update()

    @property
    def mask(self) -> np.ndarray:
        return self.canvas.mask
