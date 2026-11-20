"""비디오 셀 위젯과 카메라 설정 다이얼로그."""
from __future__ import annotations

from urllib.parse import quote

import time

from PyQt6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget,
    QVBoxLayout, QWidget,
)

from .config import MAIN, SUB, VENDORS, AppConfig, Camera
from .stream import StreamWorker


class _Bridge(QObject):
    """워커 스레드 -> GUI 스레드 전달용. 크로스 스레드 emit 은 자동으로 큐잉된다."""

    frame_ready = pyqtSignal(object)   # worker
    status = pyqtSignal(object, str)   # worker, text


class VideoCell(QWidget):
    double_clicked = pyqtSignal(int)
    edit_mask = pyqtSignal(int)

    BG = QColor(12, 12, 12)
    LABEL_FONT = QFont("Segoe UI", 9)
    DET_FONT = QFont("Segoe UI", 9, QFont.Weight.Bold)

    def __init__(self, index: int, name: str, parent=None) -> None:
        super().__init__(parent)
        self.index = index
        self.name = name
        self.worker: StreamWorker | None = None    # 화면에 보이는 스트림
        self._pending: StreamWorker | None = None  # 전환 중 뒤에서 연결 중인 새 스트림
        self.stream = SUB
        self.max_fps = 0
        self.show_stats = False
        self.show_stationary = False  # 제자리 물체(움직임 미확정) 박스도 그릴지
        self.mask = None              # 감지 제외 영역 (정보 표시 켜면 반투명 빨강으로 보임)
        self._image: QImage | None = None
        self._status = ""
        self._dets: list = []       # 최근 AI 감지 결과 (detect.Detection)
        self._dets_time = 0.0
        self._label_cache: tuple[str, float, QPixmap] | None = None  # (글자, DPR, 그려 둔 이름표)
        self._mask_cache: tuple[object, QImage] | None = None       # (마스크, 반투명 오버레이)
        self._bridge = _Bridge()
        self._bridge.frame_ready.connect(self._on_frame_ready)
        self._bridge.status.connect(self._on_status)

        # 배경을 직접 칠하므로 Qt 의 배경 지우기 생략
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(160, 90)

    # ---- 스트림 제어 ---------------------------------------------------------
    def start(self, url: str, stream: str, *, hwaccel, transport, max_fps, decode_threads, jitter_max,
              detector=None) -> None:
        """스트림 시작/전환.

        끊김 없는 전환: 이미 영상이 나오고 있으면 기존 스트림을 계속 재생하면서 새 스트림을 뒤에서 연결하고,
        새 스트림의 첫 프레임이 도착하는 순간 교체한다. 새 연결은 키프레임을 기다리느라 1~5초 걸리므로
        (무선 카메라 실측) 그동안 멈춘/검은 화면 대신 기존 영상이 계속 움직인다.
        """
        self.stream = stream
        self.max_fps = max_fps
        w = StreamWorker(
            url, hwaccel=hwaccel, transport=transport, max_fps=max_fps,
            decode_threads=decode_threads,
            jitter_max=jitter_max,
            detector=detector,
            cam_id=self.index,
            on_frame=lambda: self._bridge.frame_ready.emit(w),
            on_status=lambda s: self._bridge.status.emit(w, s),
        )
        if self.worker is not None and self._image is not None:
            if self._pending is not None:
                self._pending.stop()
            self._pending = w
            # 새 스트림이 끝내 안 나오면(주소/권한 오류 등) 오류 상태를 보여주도록 일정 시간 뒤 강제 교체
            QTimer.singleShot(8000, lambda w=w: self._promote(w))
        else:
            self._stop_worker()
            self.worker = w
        self._push_box()
        w.start()

    def _promote(self, w: StreamWorker) -> None:
        if w is not self._pending:
            return
        old, self.worker, self._pending = self.worker, w, None
        if old is not None:
            old.stop()
        self._status = ""

    def ready(self) -> bool:
        """전환 대기 없이 현재 스트림 영상이 나오는 중인가 (자동 확대 시간 계산용)."""
        return self._pending is None and self.worker is not None and self._image is not None \
            and self.worker.src_size[0] > 0

    def workers(self) -> list[StreamWorker]:
        return [w for w in (self.worker, self._pending) if w is not None]

    def set_max_fps(self, fps: int) -> None:
        """재연결 없이 FPS 상한만 변경 (그리드 15 <-> 확대 30)."""
        self.max_fps = fps
        for w in self.workers():
            w.min_interval = 1.0 / max(1, fps)

    def _stop_worker(self) -> None:
        # join 하지 않음: 네트워크 대기 중인 스레드가 GUI 를 멈추지 않게 한다.
        # 스레드는 read timeout(최대 5초) 안에 스스로 종료된다.
        for w in self.workers():
            w.stop()
        self.worker = self._pending = None

    def stop(self) -> None:
        self._stop_worker()
        self._image = None
        self._status = ""
        self._dets = []
        self.update()

    def _on_frame_ready(self, worker: StreamWorker) -> None:
        if worker is self._pending:  # 새 스트림 첫 프레임 도착 -> 교체
            self._promote(worker)
        elif worker is not self.worker:  # 교체된 이전 워커의 늦은 프레임
            return
        img = worker.take_frame()
        if img is not None:
            # 워커가 물리 픽셀 크기로 만들었으므로 DPR 을 지정해 Qt 가 다시 스케일하지 않게 한다
            img.setDevicePixelRatio(self.devicePixelRatioF())
            self._image = img
            self.update()

    def set_detections(self, dets: list) -> None:
        if dets or self._dets:
            self._dets = dets
            self._dets_time = time.monotonic()
            self.update()

    def _active_dets(self) -> list:
        # 결과가 2초 넘게 갱신되지 않으면(카메라 끊김 등) 박스를 지운다
        if time.monotonic() - self._dets_time >= 2.0:
            return []
        return self._dets if self.show_stationary else [d for d in self._dets if not d.stationary]

    def _paint_dets(self, p: QPainter, x: float, y: float, w: float, h: float) -> None:
        from .detect import GROUP_COLORS
        p.setFont(self.DET_FONT)
        fm = p.fontMetrics()
        for d in self._active_dets():
            color = QColor(*GROUP_COLORS[d.group])
            if d.stationary:
                color.setAlpha(110)
            r = QRectF(x + d.box[0] * w, y + d.box[1] * h, (d.box[2] - d.box[0]) * w, (d.box[3] - d.box[1]) * h)
            p.setPen(QPen(color, 1 if d.stationary else 2,
                          Qt.PenStyle.DashLine if d.stationary else Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            text = f"{d.label} {d.score * 100:.0f}%" + (" (정지)" if d.stationary else "")
            tr = QRectF(r.left(), max(y, r.top() - fm.height() - 2), fm.horizontalAdvance(text) + 8, fm.height() + 2)
            p.fillRect(tr, color)
            p.setPen(QColor(0, 0, 0))
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, text)

    def snapshot(self) -> QImage | None:
        """현재 화면(감지 박스 포함)을 이미지로. 스냅샷 저장용."""
        if self._image is None:
            return None
        img = self._image.copy()
        img.setDevicePixelRatio(1.0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_dets(p, 0, 0, img.width(), img.height())
        p.end()
        return img

    def _on_status(self, worker: StreamWorker, text: str) -> None:
        if worker is self.worker:
            self._status = text
            self.update()

    # ---- Qt 이벤트 -----------------------------------------------------------
    def _push_box(self) -> None:
        dpr = self.devicePixelRatioF()
        for w in self.workers():
            w.set_box(int(self.width() * dpr), int(self.height() * dpr))

    def resizeEvent(self, e) -> None:
        self._push_box()
        super().resizeEvent(e)

    def contextMenuEvent(self, e) -> None:
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("감지 제외 영역 그리기 (펜)…", lambda: self.edit_mask.emit(self.index))
        menu.exec(e.globalPos())

    def mouseDoubleClickEvent(self, e) -> None:
        self.double_clicked.emit(self.index)

    def paintEvent(self, e) -> None:
        # 프레임마다(셀당 15~30회/초) 호출되는 경로. 실측 비용 순: 영상 복사 > 배경 칠하기 > 이름표 글자.
        # 배경은 영상이 덮지 않는 여백만 칠하고, 이름표는 글자가 바뀔 때만 다시 그린다.
        p = QPainter(self)
        rect = self.rect()
        if self._image is not None:
            size = self._image.deviceIndependentSize()
            # 정수 좌표: 소수 좌표면 Qt 가 1:1 복사 대신 보간 경로를 탈 수 있다
            x = (self.width() - round(size.width())) // 2
            y = (self.height() - round(size.height())) // 2
            img_rect = QRect(x, y, round(size.width()), round(size.height()))
            if img_rect != rect:
                for bar in (QRect(0, 0, rect.width(), y), QRect(0, img_rect.bottom() + 1, rect.width(), rect.height()),
                            QRect(0, y, x, img_rect.height()), QRect(img_rect.right() + 1, y, rect.width(), img_rect.height())):
                    if bar.isValid():
                        p.fillRect(bar, self.BG)
            p.drawImage(QPoint(x, y), self._image)
            if self.show_stats and self.mask is not None:
                p.drawImage(QRectF(img_rect), self._mask_overlay())
            dets = self._active_dets()
            if dets:
                self._paint_dets(p, x, y, size.width(), size.height())
            dets = [d for d in dets if not d.stationary]
            if dets:
                # 감지 중인 셀은 테두리 강조 (사람 > 차량 > 동물 우선)
                from .detect import GROUP_COLORS
                top = min(dets, key=lambda d: ("person", "vehicle", "animal").index(d.group)).group
                p.setPen(QPen(QColor(*GROUP_COLORS[top]), 4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(2, 2, -2, -2))
        else:
            p.fillRect(rect, self.BG)

        label = f"{self.name}  [{'MAIN' if self.stream == MAIN else 'SUB'}]"
        if self.show_stats and self.worker:
            sw, sh = self.worker.src_size
            label += (f"  {sw}x{sh}  {self.worker.render_fps:.0f}fps  {self.worker.decoder_name}"
                      f"  버퍼 {self.worker.buffer_ms:.0f}ms")
        p.drawPixmap(6, 4, self._label_pixmap(label))
        if self._status or self.worker is None:
            p.setFont(self.LABEL_FONT)
            p.setPen(QColor(230, 180, 60) if self._status else QColor(90, 90, 90))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._status or "미사용")
        p.end()

    def _label_pixmap(self, text: str) -> QPixmap:
        """반투명 배경 + 글자 이름표. 글자/DPR 이 같으면 이전에 그린 것을 재사용."""
        dpr = self.devicePixelRatioF()
        if self._label_cache and self._label_cache[:2] == (text, dpr):
            return self._label_cache[2]
        from PyQt6.QtGui import QFontMetrics
        r = QFontMetrics(self.LABEL_FONT).boundingRect(text).adjusted(-4, -2, 4, 2)
        pm = QPixmap(round(r.width() * dpr), round(r.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(0, 0, 0, 150))
        lp = QPainter(pm)
        lp.setFont(self.LABEL_FONT)
        lp.setPen(QColor(235, 235, 235))
        lp.drawText(QRect(0, 0, r.width(), r.height()), Qt.AlignmentFlag.AlignCenter, text)
        lp.end()
        self._label_cache = (text, dpr, pm)
        return pm

    def _mask_overlay(self) -> QImage:
        """감지 제외 영역 반투명 빨강 (정보 표시 켰을 때만). 마스크가 바뀔 때만 새로 만든다."""
        if self._mask_cache and self._mask_cache[0] is self.mask:
            return self._mask_cache[1]
        import numpy as np
        rgba = np.zeros(self.mask.shape + (4,), np.uint8)
        rgba[self.mask] = (230, 40, 40, 90)
        h, w = self.mask.shape
        overlay = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        self._mask_cache = (self.mask, overlay)
        return overlay


class SettingsDialog(QDialog):
    COLS = ["사용", "이름", "IP", "포트", "사용자", "비밀번호", "채널", "제조사"]

    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("카메라 설정")
        self.resize(1250, 600)
        self._cfg = cfg

        self.table = QTableWidget(len(cfg.cameras), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col in (0, 3, 6, 7):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._rows: list[dict] = []
        for r, cam in enumerate(cfg.cameras):
            self._rows.append(self._make_row(r, cam))
        self.table.currentCellChanged.connect(lambda *_: self._update_preview())

        self.preview = QLabel()
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # 일반 설정
        self.hwaccel = QComboBox()
        self.hwaccel.addItems(["auto", "none", "d3d11va", "dxva2", "cuda", "qsv"])
        self.hwaccel.setCurrentText(cfg.hwaccel)
        self.grid_quality = _resolution_combo(cfg, cfg.grid_quality)
        self.full_quality = _resolution_combo(cfg, cfg.fullscreen_quality)
        self.hw_sub = QCheckBox("서브스트림(그리드)에도 하드웨어 디코딩 적용")
        self.hw_sub.setChecked(cfg.hwaccel_on_sub)
        self.transport = QComboBox()
        self.transport.addItems(["tcp", "udp"])
        self.transport.setCurrentText(cfg.rtsp_transport)
        self.grid_fps = _spin(1, 30, cfg.grid_max_fps)
        self.full_fps = _spin(1, 60, cfg.fullscreen_max_fps)
        self.jitter = _spin(0, 3000, cfg.jitter_buffer_ms)
        self.jitter.setSingleStep(100)
        self.jitter.setSuffix(" ms")
        self.jitter.setToolTip("무선 카메라 끊김 보정. 흔들림만큼만 자동으로 지연되며 이 값이 상한.\n"
                               "0 = 끄기 (받는 즉시 표시)")

        general = QGroupBox("해상도 / 디코딩")
        f = QFormLayout(general)
        f.addRow("그리드 해상도", self.grid_quality)
        f.addRow("확대 화면 해상도", self.full_quality)
        f.addRow("하드웨어 가속", self.hwaccel)
        f.addRow("", self.hw_sub)
        f.addRow("RTSP 전송", self.transport)
        f.addRow("그리드 최대 FPS", self.grid_fps)
        f.addRow("전체화면 최대 FPS", self.full_fps)
        f.addRow("끊김 보정 버퍼 (최대)", self.jitter)

        g = cfg.go2rtc
        self.g_enabled = QCheckBox("go2rtc 릴레이 경유 (카메라 직접 접속 대신 rtsp://host:port/camN_main|sub)")
        self.g_enabled.setChecked(g.enabled)
        self.g_host = QLineEdit(g.host)
        self.g_port = _spin(1, 65535, g.port)
        self.g_user = QLineEdit(g.username)
        self.g_pass = _password(g.password)
        relay = QGroupBox("go2rtc 서버")
        f2 = QFormLayout(relay)
        f2.addRow(self.g_enabled)
        f2.addRow("호스트", self.g_host)
        f2.addRow("RTSP 포트", self.g_port)
        f2.addRow("사용자", self.g_user)
        f2.addRow("비밀번호", self.g_pass)
        self.g_enabled.toggled.connect(lambda *_: self._update_preview())

        # AI 감지
        self.ai_enabled = QCheckBox("AI 감지 사용 (화면에 표시 중인 카메라만 분석)")
        self.ai_enabled.setChecked(cfg.ai_enabled)
        self.ai_targets = {}
        targets = QHBoxLayout()
        for key, label in (("person", "사람"), ("vehicle", "차량"), ("animal", "동물")):
            cb = QCheckBox(label)
            cb.setChecked(key in cfg.ai_targets)
            self.ai_targets[key] = cb
            targets.addWidget(cb)
        self.ai_conf = _spin(20, 90, cfg.ai_confidence)
        self.ai_conf.setSuffix(" %")
        self.ai_conf.setToolTip("낮추면 더 민감(오탐 증가), 높이면 확실한 것만 감지")
        self.ai_fps = QDoubleSpinBox()
        self.ai_fps.setRange(0.5, 10)
        self.ai_fps.setSingleStep(0.5)
        self.ai_fps.setValue(cfg.ai_fps)
        self.ai_fps.setSuffix(" 장/초")
        self.ai_model = QComboBox()
        self.ai_model.addItem("빠름 (YOLOX-Nano, 기본)", "fast")
        self.ai_model.addItem("정확 (YOLOX-Tiny, 먼 거리 유리, 연산량 약 4배)", "accurate")
        self.ai_model.setCurrentIndex(max(0, self.ai_model.findData(cfg.ai_model)))
        self.ai_device = QComboBox()
        self.ai_device.addItem("자동 (GPU 우선, 없으면 CPU)", "auto")
        self.ai_device.addItem("CPU만 사용", "cpu")
        self.ai_device.setCurrentIndex(max(0, self.ai_device.findData(cfg.ai_device)))
        self.ai_sound = QCheckBox("효과음")
        self.ai_sound.setChecked(cfg.ai_sound)
        from .sound import SOUNDS
        self.ai_sound_kind = QComboBox()
        for key, label in SOUNDS.items():
            self.ai_sound_kind.addItem(label, key)
        self.ai_sound_kind.setCurrentIndex(max(0, self.ai_sound_kind.findData(cfg.ai_sound_kind)))
        self.ai_sound_vol = _spin(0, 100, cfg.ai_sound_volume)
        self.ai_sound_vol.setSuffix(" %")
        self.ai_sound_vol.setSingleStep(10)
        preview = QPushButton("미리 듣기")
        preview.clicked.connect(self._preview_sound)
        self.ai_snap = QCheckBox("스냅샷 저장")
        self.ai_snap.setChecked(cfg.ai_snapshots)
        self.ai_cooldown = _spin(3, 600, cfg.ai_cooldown_s)
        self.ai_cooldown.setSuffix(" 초")
        self.ai_show_static = QCheckBox("제자리 물체 박스도 표시 (주차된 차 등, 오탐 포함)")
        self.ai_show_static.setChecked(cfg.ai_show_stationary)
        self.ai_cooldown.setToolTip("같은 카메라에서 같은 대상이 이 시간 이상 안 보였다가 다시 나타나야 새 이벤트로 기록")
        alerts = QHBoxLayout()
        alerts.addWidget(self.ai_snap)
        sound_row = QHBoxLayout()
        sound_row.addWidget(self.ai_sound)
        sound_row.addWidget(self.ai_sound_kind, 1)
        sound_row.addWidget(self.ai_sound_vol)
        sound_row.addWidget(preview)
        self.az_enabled = QCheckBox("사용")
        self.az_enabled.setChecked(cfg.auto_zoom)
        self.az_seconds = _spin(1, 30, cfg.auto_zoom_seconds)
        self.az_seconds.setSuffix(" 초")
        self.az_targets = {}
        auto_zoom = QHBoxLayout()
        auto_zoom.addWidget(self.az_enabled)
        auto_zoom.addWidget(self.az_seconds)
        for key, label in (("person", "사람"), ("vehicle", "차량"), ("animal", "동물")):
            cb = QCheckBox(label)
            cb.setChecked(key in cfg.auto_zoom_targets)
            self.az_targets[key] = cb
            auto_zoom.addWidget(cb)
        auto_zoom.addStretch(1)
        self.az_enabled.setToolTip("새로 나타난 대상이 감지된 카메라를 확대 화면 해상도로 잠시 보여준 뒤 원래 화면으로 복귀\n"
                                   "(직접 확대해 보고 있을 때는 동작하지 않음)")
        ai = QGroupBox("AI 감지")
        f3 = QFormLayout(ai)
        f3.addRow(self.ai_enabled)
        f3.addRow("감지 대상", targets)
        f3.addRow("신뢰도 기준", self.ai_conf)
        f3.addRow("분석 속도", self.ai_fps)
        f3.addRow("모델", self.ai_model)
        f3.addRow("연산 장치", self.ai_device)
        f3.addRow("이벤트 간격", self.ai_cooldown)
        f3.addRow("", self.ai_show_static)
        f3.addRow("새 이벤트 시", alerts)
        f3.addRow("움직임 감지 소리", sound_row)
        f3.addRow("감지 시 자동 확대", auto_zoom)

        bottom = QHBoxLayout()
        bottom.addWidget(general)
        bottom.addWidget(ai)
        bottom.addWidget(relay)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        from . import __version__
        update_btn = buttons.addButton("업데이트 확인…", QDialogButtonBox.ButtonRole.ActionRole)
        update_btn.clicked.connect(self._check_update)
        version = QLabel(f"CCTV Viewer v{__version__}")
        version.setStyleSheet("color: gray")
        footer = QHBoxLayout()
        footer.addWidget(version)
        footer.addWidget(buttons, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addWidget(self.preview)
        lay.addLayout(bottom)
        lay.addLayout(footer)
        self.table.setCurrentCell(0, 1)
        self._update_preview()

    def _preview_sound(self) -> None:
        from .sound import play
        play(self.ai_sound_kind.currentData(), self.ai_sound_vol.value())

    def _check_update(self) -> None:
        from .update_ui import UpdateDialog
        UpdateDialog(self._cfg.update_url, self).exec()

    def _make_row(self, r: int, cam: Camera) -> dict:
        row = {
            "enabled": QCheckBox(),
            "name": QLineEdit(cam.name),
            "ip": QLineEdit(cam.ip),
            "port": _spin(1, 65535, cam.port),
            "username": QLineEdit(cam.username),
            "password": _password(cam.password),
            "channel": _spin(1, 64, cam.channel),
            "vendor": QComboBox(),
        }
        for key, label in VENDORS.items():
            row["vendor"].addItem(label, key)
        row["vendor"].setCurrentIndex(max(0, row["vendor"].findData(cam.vendor)))
        row["vendor"].setToolTip("Tapo: 앱 > 카메라 설정 > 고급 설정 > 카메라 계정에서 만든 계정 입력 (채널 무시)")
        row["enabled"].setChecked(cam.enabled)
        row["ip"].setPlaceholderText("192.168.0.64")
        for c, key in enumerate(row):
            w = row[key]
            if isinstance(w, QLineEdit):
                w.textChanged.connect(lambda *_: self._update_preview())
                w.setFrame(False)
            if isinstance(w, QSpinBox):
                w.valueChanged.connect(lambda *_: self._update_preview())
            if isinstance(w, QComboBox):
                w.currentIndexChanged.connect(lambda *_: self._update_preview())
            if isinstance(w, QCheckBox):  # 셀 가운데 정렬
                box = QWidget()
                hl = QHBoxLayout(box)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                hl.addWidget(w)
                w = box
            self.table.setCellWidget(r, c, w)
        return row

    def _update_preview(self) -> None:
        r = max(0, self.table.currentRow())
        cfg = self._collect()
        masked = cfg.cameras[r].password
        main, sub = cfg.stream_url(r, MAIN), cfg.stream_url(r, SUB)
        for secret in {masked, cfg.go2rtc.password} - {""}:
            main = main.replace(quote(secret, safe=""), "****")
            sub = sub.replace(quote(secret, safe=""), "****")
        self.preview.setText(f"<b>CAM {r + 1}</b>  메인: <code>{main}</code><br>"
                             f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;서브: <code>{sub}</code>")

    def _collect(self) -> AppConfig:
        cams = [
            Camera(
                name=row["name"].text().strip() or f"CAM {i + 1}",
                enabled=row["enabled"].isChecked(),
                ip=row["ip"].text().strip(),
                port=row["port"].value(),
                username=row["username"].text().strip(),
                password=row["password"].text(),
                channel=row["channel"].value(),
                vendor=row["vendor"].currentData(),
                ai_mask=self._cfg.cameras[i].ai_mask,  # 펜으로 칠한 영역은 설정 표에 없으므로 그대로 유지
            )
            for i, row in enumerate(self._rows)
        ]
        cfg = AppConfig.from_dict(self._cfg.to_dict())
        cfg.cameras = cams
        cfg.grid_quality = self.grid_quality.currentData()
        cfg.fullscreen_quality = self.full_quality.currentData()
        cfg.ai_enabled = self.ai_enabled.isChecked()
        cfg.ai_targets = [k for k, cb in self.ai_targets.items() if cb.isChecked()]
        cfg.ai_confidence = self.ai_conf.value()
        cfg.ai_fps = self.ai_fps.value()
        cfg.ai_model = self.ai_model.currentData()
        cfg.ai_device = self.ai_device.currentData()
        cfg.ai_sound = self.ai_sound.isChecked()
        cfg.ai_sound_kind = self.ai_sound_kind.currentData()
        cfg.ai_sound_volume = self.ai_sound_vol.value()
        cfg.ai_snapshots = self.ai_snap.isChecked()
        cfg.ai_cooldown_s = self.ai_cooldown.value()
        cfg.ai_show_stationary = self.ai_show_static.isChecked()
        cfg.auto_zoom = self.az_enabled.isChecked()
        cfg.auto_zoom_seconds = self.az_seconds.value()
        cfg.auto_zoom_targets = [k for k, cb in self.az_targets.items() if cb.isChecked()]
        cfg.hwaccel = self.hwaccel.currentText()
        cfg.hwaccel_on_sub = self.hw_sub.isChecked()
        cfg.rtsp_transport = self.transport.currentText()
        cfg.grid_max_fps = self.grid_fps.value()
        cfg.fullscreen_max_fps = self.full_fps.value()
        cfg.jitter_buffer_ms = self.jitter.value()
        g = cfg.go2rtc
        g.enabled = self.g_enabled.isChecked()
        g.host = self.g_host.text().strip()
        g.port = self.g_port.value()
        g.username = self.g_user.text().strip()
        g.password = self.g_pass.text()
        return cfg

    def result_config(self) -> AppConfig:
        return self._collect()


def _resolution_combo(cfg: AppConfig, current: str) -> QComboBox:
    """'1920 x 1080 (메인스트림)' 처럼 실제 해상도를 보여준다 (한 번이라도 연결된 적이 있으면)."""
    box = QComboBox()
    for key, name, hint in ((MAIN, "메인스트림", "고화질"), (SUB, "서브스트림", "저부하")):
        size = cfg.seen_resolutions.get(key)
        label = f"{size[0]} x {size[1]}  ({name}, {hint})" if size else f"{name} ({hint})"
        box.addItem(label, key)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


def _spin(lo: int, hi: int, val: int) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def _password(text: str) -> QLineEdit:
    e = QLineEdit(text)
    e.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
    return e
