"""NVR 녹화 재생 창 + PC 저장 창.

- 재생: 이벤트 시각 전후를 NVR 하드디스크에서 바로 재생 (PC 에 저장하지 않음)
- PC에 저장: 사용자가 [저장] 을 눌렀을 때만 해당 구간을 MP4 로 받는다 (수동)
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QDateTime, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QVBoxLayout,
)

from .config import MAIN, TAPO, AppConfig
from .stream import resolve_hwaccel
from .export import ClipExporter, clip_window, default_clip_path
from .widgets import VideoCell

PRE_ROLL = timedelta(seconds=10)  # 감지 시각보다 이만큼 앞에서 재생 시작


class _ExportBridge(QObject):
    progress = pyqtSignal(float)
    done = pyqtSignal(bool, str)


class ExportDialog(QDialog):
    """NVR 녹화 구간을 PC 에 MP4 로 저장 (사용자가 [저장] 을 눌러야 시작)."""

    def __init__(self, cfg: AppConfig, cam: int, start: datetime, seconds: int, clips_dir: Path,
                 parent=None, current_pos: datetime | None = None) -> None:
        super().__init__(parent)
        self.cfg, self.cam, self.clips_dir = cfg, cam, clips_dir
        self.cam_name = cfg.cameras[cam].name
        self.setWindowTitle(f"PC에 저장 — {self.cam_name}")
        self.setMinimumWidth(560)
        self._exporter: ClipExporter | None = None
        self._saved: Path | None = None
        self._custom_path = False

        self.start = QDateTimeEdit(QDateTime(start))
        self.start.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start.setCalendarPopup(True)
        self.length = QSpinBox()
        self.length.setRange(5, 600)
        self.length.setValue(seconds)
        self.length.setSuffix(" 초")
        self.path = QLineEdit(str(default_clip_path(clips_dir, self.cam_name, start)))
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        self.start.dateTimeChanged.connect(self._auto_path)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        start_row = QHBoxLayout()
        start_row.addWidget(self.start, 1)
        if current_pos is not None:
            here = QPushButton("현재 재생 위치")
            here.clicked.connect(lambda: self.start.setDateTime(QDateTime(current_pos)))
            start_row.addWidget(here)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.info = QLabel("NVR 이 녹화를 실제 속도로 보내므로 저장에 구간 길이만큼 시간이 걸립니다 (30초 → 약 30초).")
        self.info.setWordWrap(True)

        self.save_btn = QPushButton("저장")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)
        self.open_btn = QPushButton("폴더 열기")
        self.open_btn.clicked.connect(self._open_folder)
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)

        form = QFormLayout()
        form.addRow("카메라", QLabel(self.cam_name))
        form.addRow("시작 시각", start_row)
        form.addRow("길이", self.length)
        form.addRow("저장 위치", path_row)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.bar)
        lay.addWidget(self.info)
        lay.addLayout(buttons)

        self._bridge = _ExportBridge()
        self._bridge.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self._bridge.done.connect(self._finished)

    def _auto_path(self) -> None:
        # 사용자가 경로를 직접 고르지 않았다면 시작 시각에 맞춰 기본 파일 이름 갱신
        if not self._custom_path:
            start = self.start.dateTime().toPyDateTime()
            self.path.setText(str(default_clip_path(self.clips_dir, self.cam_name, start)))

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "저장 위치", self.path.text(), "MP4 동영상 (*.mp4)")
        if path:
            self._custom_path = True
            self.path.setText(path if path.lower().endswith(".mp4") else path + ".mp4")

    def _save(self) -> None:
        cam = self.cfg.cameras[self.cam]
        if not cam.ip:
            QMessageBox.warning(self, "PC에 저장", "카메라 IP 가 설정되지 않아 NVR 녹화를 받을 수 없습니다.")
            return
        if cam.vendor == TAPO:
            QMessageBox.warning(self, "PC에 저장", "Tapo 카메라는 RTSP 녹화 재생을 지원하지 않습니다 (SD 카드 녹화는 Tapo 앱에서 확인).")
            return
        start = self.start.dateTime().toPyDateTime()
        if start > datetime.now() - timedelta(seconds=5):
            QMessageBox.warning(self, "PC에 저장", "시작 시각이 현재보다 늦습니다.")
            return
        out = Path(self.path.text())
        if out.exists() and QMessageBox.question(self, "PC에 저장", f"이미 있는 파일입니다. 덮어쓸까요?\n{out}") \
                != QMessageBox.StandardButton.Yes:
            return
        self._exporter = ClipExporter(cam.playback_url(start), self.length.value(), out,
                                      self._bridge.progress.emit, self._bridge.done.emit)
        self._saved = None
        for w in (self.start, self.length, self.path, self.save_btn):
            w.setEnabled(False)
        self.close_btn.setText("취소")
        self.bar.setValue(0)
        self.bar.show()
        self.info.setText("NVR 에서 녹화를 받는 중…")
        self._exporter.start()

    def _finished(self, ok: bool, msg: str) -> None:
        self._exporter = None
        for w in (self.start, self.length, self.path, self.save_btn):
            w.setEnabled(True)
        self.close_btn.setText("닫기")
        if ok:
            self.bar.setValue(1000)
            self._saved = Path(self.path.text())
            self.info.setText(f"저장 완료: {msg}")
        else:
            self.bar.hide()
            self.info.setText(msg)

    def _open_folder(self) -> None:
        target = self._saved or Path(self.path.text())
        folder = target.parent
        folder.mkdir(parents=True, exist_ok=True)
        if self._saved and os.name == "nt":
            # 탐색기에서 저장한 파일을 선택한 상태로 열기
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(self._saved)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def reject(self) -> None:
        """Esc / [닫기]·[취소] / 창 X 버튼 모두 여기로 온다 (QDialog.closeEvent 가 reject 를 호출).
        close() 를 다시 부르면 closeEvent <-> reject 무한 재귀가 되므로 부모 구현으로만 닫는다."""
        if self._exporter is not None:
            if QMessageBox.question(self, "PC에 저장", "저장 중입니다. 취소할까요?") != QMessageBox.StandardButton.Yes:
                return
            self._exporter.cancel()
        super().reject()


class PlaybackDialog(QDialog):
    def __init__(self, cfg: AppConfig, cam: int, event_time: datetime, clips_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.cfg, self.cam, self.event_time, self.clips_dir = cfg, cam, event_time, clips_dir
        name = cfg.cameras[cam].name
        self.setWindowTitle(f"녹화 재생 — {name}  (감지 {event_time:%Y-%m-%d %H:%M:%S})")
        self.resize(1100, 680)

        self.view = VideoCell(cam, f"{name}  녹화")
        self.pos_label = QLabel()
        buttons = QHBoxLayout()
        for text, delta in (("◀ 30초", -30), ("◀ 5초", -5), ("5초 ▶", 5), ("30초 ▶", 30)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, d=delta: self.seek(self.position() + timedelta(seconds=d)))
            buttons.addWidget(b)
        again = QPushButton("감지 시점부터 다시")
        again.clicked.connect(lambda: self.seek(self.event_time - PRE_ROLL))
        buttons.addWidget(again)
        save = QPushButton("💾 PC에 저장")
        save.clicked.connect(self._export)
        buttons.addWidget(save)
        buttons.addStretch(1)
        buttons.addWidget(self.pos_label)

        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(buttons)

        self._start = self.event_time - PRE_ROLL
        self._started_wall: float | None = None  # 첫 프레임이 화면에 나온 시각
        self._timer = QTimer(self, interval=250)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.seek(self._start)

    def position(self) -> datetime:
        """현재 재생 위치 (첫 프레임 이후 경과 시간으로 추정)."""
        if self._started_wall is None:
            return self._start
        return self._start + timedelta(seconds=time.monotonic() - self._started_wall)

    def seek(self, when: datetime) -> None:
        when = min(when, datetime.now() - timedelta(seconds=5))
        cam = self.cfg.cameras[self.cam]
        if not cam.ip:
            self.pos_label.setText("카메라 IP 가 설정되지 않아 녹화를 재생할 수 없습니다 (go2rtc 모드는 NVR 직접 접속 정보 필요)")
            return
        if cam.vendor == TAPO:
            self.pos_label.setText("Tapo 카메라는 RTSP 녹화 재생을 지원하지 않습니다 (SD 카드 녹화는 Tapo 앱에서 확인)")
            return
        self._start, self._started_wall = when, None
        self.view.start(
            cam.playback_url(when), MAIN,
            hwaccel=resolve_hwaccel(self.cfg.hwaccel), transport=self.cfg.rtsp_transport,
            max_fps=self.cfg.fullscreen_max_fps, decode_threads=0,
            jitter_max=self.cfg.jitter_buffer_ms / 1000,
        )

    def _export(self) -> None:
        start, seconds = clip_window(self.event_time)
        dlg = ExportDialog(self.cfg, self.cam, start, seconds, self.clips_dir, self, current_pos=self.position())
        dlg.show()

    def _tick(self) -> None:
        if self._started_wall is None and self.view._image is not None:
            self._started_wall = time.monotonic()
        if self._started_wall is None:
            self.pos_label.setText(f"NVR 녹화 불러오는 중…  ({self._start:%H:%M:%S})")
        else:
            pos = self.position()
            diff = (pos - self.event_time).total_seconds()
            self.pos_label.setText(f"재생 위치 {pos:%H:%M:%S}  (감지 {'+' if diff >= 0 else '-'}{abs(diff):.0f}초)")

    def closeEvent(self, e) -> None:
        self._timer.stop()
        self.view.stop()
        super().closeEvent(e)

    def reject(self) -> None:  # Esc
        self.view.stop()
        super().reject()
