"""AI 감지 결과 처리: 셀 표시 연결, 이벤트 목록, 스냅샷 저장, 알림음."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDockWidget, QHBoxLayout, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QVBoxLayout, QWidget,
)

from . import mask as mask_mod
from . import sound
from .config import AppConfig
from .detect import iou

log = logging.getLogger(__name__)

MAX_EVENTS = 200


class _Bridge(QObject):
    result = pyqtSignal(int, object, float)  # cam, detections, ts (엔진 스레드 -> GUI)


class _Track:
    """카메라별 물체 추적 (IoU 매칭).

    이벤트는 '실제로 움직인' 대상에만 만든다 (Frigate 방식):
    - 2회 이상 연속 감지 + 처음 위치에서 의미 있게 이동해야 확정(alerted)
    - AI 가 드럼통/잡동사니를 차량으로 잘못 잡아도 제자리에 있으면 이벤트가 되지 않는다
    - 주차된 차는 가만히 있는 동안 무시되고, 실제로 출발하면 이벤트가 된다
    """

    MIN_HITS = 2
    YOUNG = 10.0    # 처음 본 뒤 이 시간(초) 동안은 움직임 확정 여부를 보려고 AI 를 계속 돌린다
    MEMORY = 600.0  # 확정되지 않은(움직이지 않은) 물체는 10분간 기억 -> 간헐 감지로 인한 재평가 방지

    __slots__ = ("group", "box", "first_box", "first_seen", "last_seen", "hits", "alerted")

    def __init__(self, group: str, box: tuple, now: float) -> None:
        self.group, self.box, self.first_box = group, box, box
        self.first_seen = self.last_seen = now
        self.hits = 1
        self.alerted = False

    def moved(self) -> bool:
        """처음 위치 대비 이동 여부. 정지 물체의 박스 흔들림(IoU 0.8 이상, 중심 10% 미만)과 구분."""
        a, b = self.first_box, self.box
        size = max(a[2] - a[0], a[3] - a[1], 1e-6)
        shift = max(abs((a[0] + a[2]) - (b[0] + b[2])), abs((a[1] + a[3]) - (b[1] + b[3]))) / 2
        return iou(a, b) < 0.6 or shift > 0.3 * size


class AiController(QObject):
    """감지 엔진 수명 관리 + 결과를 셀/이벤트로 연결."""

    event_added = pyqtSignal(int, str, str, int, object)  # cam, 그룹, 설명, 대상 수, 스냅샷 경로(Path|None)

    def __init__(self, cells, snapshot_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.cells = cells
        self.snapshot_dir = snapshot_dir
        self.engine = None
        self.error = ""
        self._model = None
        self._device = None
        self._bridge = _Bridge()
        self._bridge.result.connect(self._on_result)
        self._tracks: dict[int, list[_Track]] = {}
        self._last_emit: dict[tuple[int, str], float] = {}
        self._last_sound = -1e9
        self.masks: dict = {}
        self.cfg: AppConfig | None = None

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.masks = {i: m for i, c in enumerate(cfg.cameras) if (m := mask_mod.decode(c.ai_mask)) is not None}
        for i, c in enumerate(self.cells):
            c.mask = self.masks.get(i)
        if not cfg.ai_enabled:
            self._set_engine(None)
            return
        if self.engine is None or self._model != cfg.ai_model or self._device != cfg.ai_device:
            try:
                from .detect import DetectionEngine
                self._set_engine(DetectionEngine(lambda c, d, t: self._bridge.result.emit(c, d, t),
                                                 model=cfg.ai_model, device=cfg.ai_device))
                self._model = cfg.ai_model
                self._device = cfg.ai_device
                self.error = ""
            except Exception as e:  # onnxruntime/모델 파일 없음 등 -> 뷰어는 계속 동작
                log.warning("AI 감지 초기화 실패: %s", e)
                self.error = str(e)
                self._set_engine(None)
                return
        self.engine.configure(cfg.ai_targets, cfg.ai_confidence / 100, cfg.ai_fps)
        for i in range(len(cfg.cameras)):
            self.engine.set_mask(i, self.masks.get(i))

    def _set_engine(self, engine) -> None:
        if self.engine is not None and self.engine is not engine:
            self.engine.stop()
        self.engine = engine
        for c in self.cells:  # 실행 중인 스트림에 즉시 반영 (재연결 없음)
            for w in c.workers():
                w.detector = engine
            if engine is None:
                c.set_detections([])

    def shutdown(self) -> None:
        self._set_engine(None)

    def _on_result(self, cam: int, dets: list, ts: float) -> None:
        if self.engine is None or cam >= len(self.cells):
            return
        cell = self.cells[cam]
        if cell.worker is None:
            return
        now = time.monotonic()
        dets = mask_mod.filter_dets(self.masks.get(cam), dets)  # 펜으로 칠한 감지 제외 영역
        cell.set_detections(dets)
        if not dets:
            return
        new_groups, hold = self._track(cam, dets, now)
        if hold:
            self.engine.hold(cam)
        for group in new_groups:
            # 같은 카메라/같은 종류가 거의 동시에 여러 개 확정되면 이벤트 1건으로 (목록 중복 방지)
            if now - self._last_emit.get((cam, group), -1e9) < 2.0:
                continue
            self._last_emit[(cam, group)] = now
            items = [d for d in dets if d.group == group]
            mixed = len({d.label for d in items}) > 1
            label = "차량" if mixed and group == "vehicle" else items[0].label
            path = self._save_snapshot(cell, group) if self.cfg.ai_snapshots else None
            # 효과음은 한 번만: 여러 카메라/종류가 동시에 확정돼도 1.5초 안에는 1회
            if self.cfg.ai_sound and now - self._last_sound >= 1.5:
                self._last_sound = now
                sound.play(self.cfg.ai_sound_kind, self.cfg.ai_sound_volume)
            self.event_added.emit(cam, group, label, len(items), path)

    def _track(self, cam: int, dets: list, now: float) -> tuple[set[str], bool]:
        """감지 결과를 기존 추적과 매칭하고, (이번에 '움직임이 확정된' 대상의 그룹, AI 연속 분석 필요 여부)를 반환.
        - 확정된 대상은 계속 보이는 동안 이벤트를 다시 만들지 않는다.
        - 확정된 대상이 이벤트 간격(쿨다운) 이상 안 보였다가 다시 나타나면 새 대상으로 본다.
        - 연속 분석은 움직인 대상이나 막 나타난 대상이 있을 때만. 오래 제자리인 물체는 움직임이 있을 때만 분석."""
        tracks = self._tracks.setdefault(cam, [])
        tracks[:] = [t for t in tracks
                     if now - t.last_seen <= (self.cfg.ai_cooldown_s if t.alerted else t.MEMORY)]
        new_groups: set[str] = set()
        used: set[int] = set()
        hold = False
        for d in sorted(dets, key=lambda d: -d.score):
            best, best_iou = None, 0.2
            for i, t in enumerate(tracks):
                if i not in used and t.group == d.group and (v := iou(t.box, d.box)) > best_iou:
                    best, best_iou = i, v
            if best is None:
                tracks.append(_Track(d.group, d.box, now))
                used.add(len(tracks) - 1)
                d.stationary = True  # 아직 움직임 미확정 -> 흐린 점선 표시
                hold = True
                continue
            t = tracks[best]
            # 직전 대비 박스가 움직였거나 막 나타난 대상만 연속 분석 (들어와서 주차한 차는 곧 움직임 대기로 전환)
            hold = hold or best_iou < 0.8 or now - t.first_seen < t.YOUNG  # 0.8: 정지 물체 박스 흔들림 범위(moved 참고)
            t.box, t.last_seen = d.box, now
            t.hits += 1
            used.add(best)
            if not t.alerted and t.hits >= t.MIN_HITS and t.moved():
                t.alerted = True
                new_groups.add(d.group)
            d.stationary = not t.alerted
        return new_groups, hold

    def _save_snapshot(self, cell, group: str) -> Path | None:
        img = cell.snapshot()
        if img is None:
            return None
        now = datetime.now()
        folder = self.snapshot_dir / now.strftime("%Y-%m-%d")
        name = re.sub(r'[\\/:*?"<>|\s]+', "_", cell.name) or f"CAM{cell.index + 1}"
        path = folder / f"{now:%H%M%S}_{name}_{group}.jpg"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if img.save(str(path), "JPG", 85):
                return path
        except OSError as e:
            log.warning("스냅샷 저장 실패: %s", e)
        return None


class EventPanel(QDockWidget):
    """오른쪽 이벤트 목록. 항목 더블클릭 -> 해당 카메라 확대."""

    focus_camera = pyqtSignal(int)
    play_recording = pyqtSignal(int, object)  # cam, datetime (감지 시각)
    save_clip = pyqtSignal(int, object)       # cam, datetime -> PC에 저장 창

    def __init__(self, snapshot_dir: Path, parent=None) -> None:
        super().__init__("감지 이벤트", parent)
        self.setObjectName("events")
        self.snapshot_dir = snapshot_dir
        self.list = QListWidget()
        self.list.setIconSize(QSize(96, 54))
        self.list.itemDoubleClicked.connect(self._play)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.setToolTip("더블클릭: NVR 녹화 재생 (감지 10초 전부터)\n우클릭: 라이브 확대 / 스냅샷 열기")

        play = QPushButton("녹화 보기")
        play.clicked.connect(lambda: self.list.currentItem() and self._play(self.list.currentItem()))
        open_dir = QPushButton("스냅샷 폴더")
        open_dir.clicked.connect(self._open_folder)
        clear = QPushButton("목록 지우기")
        clear.clicked.connect(self.list.clear)
        buttons = QHBoxLayout()
        buttons.addWidget(play)
        buttons.addWidget(open_dir)
        buttons.addWidget(clear)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.list)
        lay.addLayout(buttons)
        self.setWidget(body)
        self.setMinimumWidth(260)

    def add(self, cam: int, cam_name: str, label: str, count: int, path: Path | None) -> None:
        when = datetime.now()
        text = f"{when:%H:%M:%S}  {cam_name}\n{label}" + (f" {count}" + ("명" if label == "사람" else "개") if count > 1 else "")
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, (cam, str(path) if path else "", when))
        if path:
            item.setIcon(QIcon(QPixmap(str(path)).scaled(96, 54, Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation)))
            item.setToolTip(str(path))
        self.list.insertItem(0, item)
        while self.list.count() > MAX_EVENTS:
            self.list.takeItem(self.list.count() - 1)

    def _play(self, item: QListWidgetItem) -> None:
        cam, _, when = item.data(Qt.ItemDataRole.UserRole)
        self.play_recording.emit(cam, when)

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        cam, path, when = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("녹화 보기 (NVR)", lambda: self.play_recording.emit(cam, when))
        menu.addAction("PC에 저장 (감지 10초 전부터 30초)…", lambda: self.save_clip.emit(cam, when))
        menu.addAction("라이브 확대", lambda: self.focus_camera.emit(cam))
        snap = menu.addAction("스냅샷 열기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        snap.setEnabled(bool(path))
        menu.exec(self.list.mapToGlobal(pos))

    def _open_folder(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(self.snapshot_dir)))
