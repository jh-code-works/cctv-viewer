"""메인 윈도우: 그리드 레이아웃, 더블클릭 확대, 페이지 전환, 스트림 수명 관리."""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import QGridLayout, QLabel, QMainWindow, QMessageBox, QToolBar, QWidget

from .config import LAYOUTS, MAIN, SUB, AppConfig, save_config

from .events import AiController, EventPanel
from .stream import resolve_hwaccel
from .widgets import SettingsDialog, VideoCell


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig, config_path: Path) -> None:
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.focused: int | None = None  # 더블클릭으로 확대된 카메라
        self.page = 0

        self.setWindowTitle("CCTV Viewer")
        self.resize(1280, 760)

        self.cells = [VideoCell(i, c.name) for i, c in enumerate(cfg.cameras)]
        for cell in self.cells:
            cell.double_clicked.connect(self.toggle_focus)
            cell.edit_mask.connect(self._edit_mask)

        central = QWidget()
        central.setStyleSheet("background:#000")
        self.grid = QGridLayout(central)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(2)
        self.setCentralWidget(central)

        # AI 감지: 스냅샷은 config.json 옆 snapshots 폴더 (포터블=exe 옆, 설치형=%APPDATA%)
        snap_dir = config_path.parent / "snapshots"
        self.ai = AiController(self.cells, snap_dir, self)
        self.events = EventPanel(snap_dir, self)
        self.events.focus_camera.connect(self._focus_from_event)
        self.events.play_recording.connect(self._play_recording)
        self.events.save_clip.connect(self._save_clip)
        self.clips_dir = config_path.parent / "clips"  # 수동 저장 영상 기본 위치
        self.ai.event_added.connect(
            lambda cam, group, label, n, path: self.events.add(cam, self.cfg.cameras[cam].name, label, n, path))
        self.ai.event_added.connect(lambda cam, group, *_: self._auto_zoom(cam, group))

        # 감지 시 자동 확대 상태
        self._auto_cam: int | None = None   # 자동 확대 중인 카메라 (수동 확대와 구분)
        self._keep_streams = False          # 자동 확대 중에는 다른 채널 연결 유지 -> 복귀가 즉시
        self._auto_timer = QTimer(self, singleShot=True)
        self._auto_timer.timeout.connect(self._end_auto_zoom)
        # 확대 스트림(메인)이 실제로 화면에 나온 뒤부터 시간을 센다 (연결에 1~5초 걸릴 수 있음)
        self._auto_wait = QTimer(self, interval=100)
        self._auto_wait.timeout.connect(self._auto_wait_tick)
        self._auto_wait_start = 0.0
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.events)

        self._build_toolbar()
        self._apply_ai()
        self.apply()

    # ---- UI ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        group = QActionGroup(self)
        self.layout_actions: dict[str, QAction] = {}
        for name, key in (("2x2", "Ctrl+2"), ("3x2", "Ctrl+6"), ("3x3", "Ctrl+3")):
            act = QAction(name, self, checkable=True, shortcut=key)
            act.setToolTip(f"{name} ({key})")
            group.addAction(act)
            act.triggered.connect(lambda _=False, name=name: self.set_layout(name))
            act.setChecked(name == self.cfg.layout)
            self.layout_actions[name] = act
            tb.addAction(act)

        tb.addSeparator()
        prev_ = QAction("◀ 이전", self, shortcut=QKeySequence(Qt.Key.Key_PageUp))
        next_ = QAction("다음 ▶", self, shortcut=QKeySequence(Qt.Key.Key_PageDown))
        prev_.triggered.connect(lambda: self.turn_page(-1))
        next_.triggered.connect(lambda: self.turn_page(1))
        tb.addActions([prev_, next_])
        self.page_label = QLabel()
        self.page_label.setContentsMargins(8, 0, 8, 0)
        tb.addWidget(self.page_label)

        tb.addSeparator()
        stats = QAction("정보 표시", self, checkable=True, shortcut="I")
        stats.toggled.connect(self._toggle_stats)
        full = QAction("전체화면 (F11)", self, shortcut="F11")
        full.triggered.connect(self._toggle_window_fullscreen)
        settings = QAction("설정", self, shortcut="Ctrl+,")
        settings.triggered.connect(self.open_settings)
        tb.addActions([stats, full, settings])

        tb.addSeparator()
        self.act_ai = QAction("AI 감지", self, checkable=True, shortcut="Ctrl+D")
        self.act_ai.setChecked(self.cfg.ai_enabled)
        self.act_ai.toggled.connect(self._toggle_ai)
        events = self.events.toggleViewAction()
        events.setText("이벤트 목록")
        events.setShortcut("Ctrl+E")
        tb.addActions([self.act_ai, events])

        back = QAction(self, shortcut=QKeySequence(Qt.Key.Key_Escape))
        back.triggered.connect(self._on_escape)
        self.addAction(back)

    def _toggle_ai(self, on: bool) -> None:
        if on != self.cfg.ai_enabled:
            self.cfg.ai_enabled = on
            self._save()
            self._apply_ai()

    def _apply_ai(self) -> None:
        self.ai.apply_config(self.cfg)
        for c in self.cells:
            c.show_stationary = self.cfg.ai_show_stationary
            c.update()
        if self.cfg.ai_enabled and self.ai.engine is None:
            QMessageBox.warning(self, "AI 감지", f"AI 감지를 시작하지 못했습니다.\n{self.ai.error}")
        if hasattr(self, "act_ai"):
            self.act_ai.blockSignals(True)
            self.act_ai.setChecked(self.ai.engine is not None)
            self.act_ai.blockSignals(False)

    def _play_recording(self, cam: int, when) -> None:
        from .playback import PlaybackDialog
        dlg = PlaybackDialog(self.cfg, cam, when, self.clips_dir, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()  # 비모달: 라이브 화면을 보면서 여러 이벤트를 동시에 열 수 있음

    def _save_clip(self, cam: int, when) -> None:
        from .export import clip_window
        from .playback import ExportDialog
        start, seconds = clip_window(when)
        dlg = ExportDialog(self.cfg, cam, start, seconds, self.clips_dir, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def _edit_mask(self, cam: int) -> None:
        from . import mask as mask_mod
        from .mask_editor import MaskEditor
        cell = self.cells[cam]
        image = None
        if cell._image is not None:
            image = cell._image.copy()
            image.setDevicePixelRatio(1.0)
        dlg = MaskEditor(self.cfg.cameras[cam].name, image, mask_mod.decode(self.cfg.cameras[cam].ai_mask), self)
        if dlg.exec():
            self.cfg.cameras[cam].ai_mask = mask_mod.encode(dlg.mask)
            self._save()
            self._apply_ai()

    def _focus_from_event(self, cam: int) -> None:
        if self.focused != cam:
            self.toggle_focus(cam)

    def _toggle_stats(self, on: bool) -> None:
        for c in self.cells:
            c.show_stats = on
            c.update()

    def _toggle_window_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _on_escape(self) -> None:
        if self.focused is not None:
            self.toggle_focus(self.focused)
        elif self.isFullScreen():
            self.showNormal()

    # ---- 상태 전환 -----------------------------------------------------------
    def set_layout(self, name: str) -> None:
        self._cancel_auto_zoom()
        self.cfg.layout = name
        self.layout_actions[name].setChecked(True)
        self.page = 0
        self.focused = None
        self._save()
        self.apply()

    def turn_page(self, delta: int) -> None:
        if self.focused is not None:
            return
        pages = self._page_count()
        self.page = (self.page + delta) % pages
        self.apply()

    def toggle_focus(self, index: int) -> None:
        """수동 확대/복귀 (더블클릭, Esc, 이벤트 메뉴). 진행 중인 자동 확대는 취소."""
        if index not in self.cfg.enabled_indices():
            return
        self._cancel_auto_zoom()
        self.focused = None if self.focused == index else index
        self.apply()

    # ---- 감지 시 자동 확대 ----------------------------------------------------
    def _auto_zoom(self, cam: int, group: str) -> None:
        cfg = self.cfg
        if not cfg.auto_zoom or group not in cfg.auto_zoom_targets or cam not in cfg.enabled_indices():
            return
        if self.focused is not None and self._auto_cam is None:
            return  # 사용자가 직접 확대해 보고 있는 중에는 끼어들지 않음
        self._auto_cam = cam
        self._keep_streams = True
        self.focused = cam
        self.apply()
        # 다른 채널 감지 시 그 채널로 넘어가며 시간 재시작
        self._auto_timer.stop()
        self._auto_wait_start = time.monotonic()
        self._auto_wait.start()

    def _auto_wait_tick(self) -> None:
        if self._auto_cam is None:
            self._auto_wait.stop()
            return
        # 확대 스트림이 나오기 시작했거나 6초가 지나면 카운트다운 시작
        if self.cells[self._auto_cam].ready() or time.monotonic() - self._auto_wait_start > 6.0:
            self._auto_wait.stop()
            self._auto_timer.start(max(1, self.cfg.auto_zoom_seconds) * 1000)

    def _end_auto_zoom(self) -> None:
        if self._auto_cam is None:
            return
        self._auto_cam = None
        if self.focused is not None:
            self.focused = None
            self.apply()
        self._keep_streams = False

    def _cancel_auto_zoom(self) -> None:
        self._auto_timer.stop()
        self._auto_wait.stop()
        self._auto_cam = None
        self._keep_streams = False

    def open_settings(self) -> None:
        self._remember_resolutions()
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.cfg = dlg.result_config()
            for cell, cam in zip(self.cells, self.cfg.cameras):
                cell.name = cam.name
                cell.stop()  # URL/옵션이 바뀌었을 수 있으므로 전부 재연결
            self._cancel_auto_zoom()
            self.focused = None
            self.page = 0
            self._save()
            self._apply_ai()
            self.apply()

    def _save(self) -> None:
        try:
            save_config(self.cfg, self.config_path)
        except OSError as e:
            QMessageBox.warning(self, "저장 실패", f"{self.config_path}\n{e}")

    # ---- 핵심: 보이는 셀만 스트리밍 -------------------------------------------
    def _per_page(self) -> int:
        cols, rows = LAYOUTS[self.cfg.layout]
        return cols * rows

    def _page_count(self) -> int:
        n = len(self.cfg.enabled_indices())
        return max(1, -(-n // self._per_page()))

    def apply(self) -> None:
        """현재 상태(레이아웃/페이지/확대)에 맞게 셀 배치와 스트림을 재조정.

        - 화면에 보이지 않는 카메라는 스트림을 끊어 디코딩 비용 0
        - 그리드: 서브스트림, 확대: 해당 카메라만 메인스트림
        """
        enabled = self.cfg.enabled_indices()
        if self.focused is not None:
            visible = [self.focused]
            cols = rows = 1
        else:
            per = self._per_page()
            self.page = min(self.page, self._page_count() - 1)
            visible = enabled[self.page * per:(self.page + 1) * per]
            cols, rows = LAYOUTS[self.cfg.layout]

        # 레이아웃 재구성
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        slots = visible if self.focused is not None else visible + [None] * (cols * rows - len(visible))
        for pos, idx in enumerate(slots):
            w = self.cells[idx] if idx is not None else _EmptyCell()
            self.grid.addWidget(w, pos // cols, pos % cols)
            w.show()
        for i in range(3):
            self.grid.setRowStretch(i, 1 if i < rows else 0)
            self.grid.setColumnStretch(i, 1 if i < cols else 0)

        self._visible = visible
        self._update_streams()

        if self.focused is not None:
            kind = "메인스트림" if self.cfg.fullscreen_quality == MAIN else "서브스트림"
            auto = f" · 감지 자동 확대, {self.cfg.auto_zoom_seconds}초 후 복귀" if self._auto_cam is not None else ""
            self.page_label.setText(f"{self.cfg.cameras[self.focused].name} ({kind}){auto} — Esc/더블클릭: 복귀")
        else:
            self.page_label.setText(f"{self.page + 1}/{self._page_count()} 페이지 · 카메라 {len(enabled)}대")

    def _update_streams(self) -> None:
        """보이는 셀만 스트리밍. 해상도(메인/서브)는 설정값으로 고정 (창 크기와 무관)."""
        self._remember_resolutions()  # 스트림을 끄기 전에 실제 해상도 기록
        focused = self.focused is not None
        stream = self.cfg.fullscreen_quality if focused else self.cfg.grid_quality
        fps = self.cfg.fullscreen_max_fps if focused else self.cfg.grid_max_fps
        want = {i: stream for i in self._visible}
        for i, cell in enumerate(self.cells):
            if i not in want:
                if not self._keep_streams:  # 자동 확대 중에는 숨은 채널도 연결 유지 (AI 감지도 계속)
                    cell.stop()
            elif cell.worker is None or cell.stream != want[i]:
                self._start(cell, want[i], fps)
            elif cell.max_fps != fps:
                cell.set_max_fps(fps)  # 같은 스트림이면 재연결 없이 즉시 전환

    def _remember_resolutions(self) -> None:
        """실행 중인 스트림의 실제 해상도를 기록 -> 설정 화면에 '1920x1080' 처럼 표시."""
        for c in self.cells:
            if c.worker and c._pending is None and c.worker.src_size[0]:  # 전환 중이면 이전 스트림 해상도라 제외
                self.cfg.seen_resolutions[c.stream] = list(c.worker.src_size)

    def _start(self, cell: VideoCell, stream: str, max_fps: int) -> None:
        cfg = self.cfg
        use_hw = stream == MAIN or cfg.hwaccel_on_sub
        cell.start(
            cfg.stream_url(cell.index, stream),
            stream,
            hwaccel=resolve_hwaccel(cfg.hwaccel) if use_hw else None,
            transport=cfg.rtsp_transport,
            max_fps=max_fps,
            # 메인스트림 SW 디코딩 시에만 멀티스레드. 서브스트림은 1스레드가 최저 CPU.
            decode_threads=0 if stream == MAIN else 1,
            jitter_max=cfg.jitter_buffer_ms / 1000,
            detector=self.ai.engine,
        )

    def closeEvent(self, e) -> None:
        self._remember_resolutions()
        self._save()
        self.ai.shutdown()
        for c in self.cells:
            c.stop()
        super().closeEvent(e)


class _EmptyCell(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet("background:#0c0c0c")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
