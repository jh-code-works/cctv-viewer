"""NVR 녹화 구간을 PC 에 MP4 로 저장 (수동). 재인코딩 없이 원본 H.264/H.265 를 그대로 담는다(remux).

NVR 은 RTSP 녹화 재생을 실제 속도(1배속)로 보내므로 30초 구간 저장에 약 30초가 걸린다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import av

log = logging.getLogger(__name__)


class ClipExporter(threading.Thread):
    """on_progress(0~1), on_done(ok, 메시지) 는 이 스레드에서 호출된다 (GUI 는 시그널로 받을 것)."""

    def __init__(self, url: str, duration: float, out_path: Path,
                 on_progress: Callable[[float], None], on_done: Callable[[bool, str], None]) -> None:
        super().__init__(daemon=True, name="clip-export")
        self.url, self.duration, self.out_path = url, duration, out_path
        self._on_progress, self._on_done = on_progress, on_done
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        tmp = self.out_path.with_suffix(".part.mp4")
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            got = self._remux(tmp)
            if self._cancel.is_set():
                tmp.unlink(missing_ok=True)
                self._on_done(False, "저장을 취소했습니다")
            elif got <= 0:
                tmp.unlink(missing_ok=True)
                self._on_done(False, "해당 시각의 녹화가 없습니다")
            else:
                tmp.replace(self.out_path)
                self._on_done(True, f"{self.out_path}  ({got:.0f}초)")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            log.warning("clip export failed: %s", type(e).__name__)
            # 오류 문자열에는 비밀번호가 든 URL 이 포함될 수 있어 종류만 표시
            self._on_done(False, f"저장 실패 ({type(e).__name__})")

    def _remux(self, tmp: Path) -> float:
        """반환: 저장된 길이(초)."""
        with av.open(self.url, options={"rtsp_transport": "tcp"}, timeout=(10.0, 10.0)) as src, \
                av.open(str(tmp), "w", format="mp4") as dst:
            ins = src.streams.video[0]
            outs = dst.add_stream_from_template(ins)
            if ins.codec_context.name == "hevc":
                outs.codec_tag = "hvc1"  # Windows/맥 기본 플레이어 호환 태그
            tb = ins.time_base
            first = None
            last_dts = None
            wall0 = time.monotonic()
            elapsed = 0.0
            for pkt in src.demux(ins):
                if self._cancel.is_set():
                    break
                if pkt.dts is None or pkt.size == 0:
                    continue
                if first is None:
                    if not pkt.is_keyframe:
                        continue  # 키프레임부터 시작해야 재생 가능한 파일이 된다
                    first = pkt.dts
                elapsed = float((pkt.dts - first) * tb)
                if elapsed >= self.duration:
                    break
                # NVR 이 녹화 공백을 건너뛰면 시간이 튈 수 있다 -> 너무 오래 기다리지 않게 벽시계로도 제한
                if time.monotonic() - wall0 > self.duration * 1.5 + 20:
                    break
                pkt.dts -= first
                pkt.pts = pkt.pts - first if pkt.pts is not None else pkt.dts
                if last_dts is not None and pkt.dts <= last_dts:
                    pkt.dts = last_dts + 1
                    pkt.pts = max(pkt.pts, pkt.dts)
                last_dts = pkt.dts
                pkt.stream = outs
                dst.mux(pkt)
                self._on_progress(min(1.0, elapsed / self.duration))
            return elapsed if first is not None else 0.0


def default_clip_path(base: Path, cam_name: str, start: datetime) -> Path:
    import re
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", cam_name) or "CAM"
    return base / f"{start:%Y-%m-%d}" / f"{start:%H%M%S}_{name}.mp4"


def clip_window(event_time: datetime) -> tuple[datetime, int]:
    """이벤트 기본 저장 구간: 감지 10초 전부터 30초."""
    return event_time - timedelta(seconds=10), 30
