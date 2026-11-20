"""감지 알림 효과음. 소리 파일을 번들하지 않고 파형을 직접 생성해 WAV 로 재생한다.

- Windows: winsound (표준 라이브러리, 비동기)
- Linux  : paplay(PulseAudio/PipeWire) -> pw-play -> aplay 순으로 사용 가능한 것
Qt Multimedia 를 쓰지 않아 exe 크기가 늘지 않는다.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 44100
SOUNDS = {"chime": "딩동 (차임)", "beep": "삐 (단음)", "buzzer": "삐삐 (부저)"}

_cache: dict[tuple[str, int], Path] = {}
_lock = threading.Lock()


def _tone(freq: float, dur: float, decay: float = 0.0, square: bool = False) -> np.ndarray:
    t = np.arange(int(RATE * dur)) / RATE
    wave_ = np.sign(np.sin(2 * np.pi * freq * t)) * 0.6 if square else np.sin(2 * np.pi * freq * t)
    if not square:  # 배음을 조금 섞어 스피커에서 또렷하게
        wave_ = wave_ + 0.3 * np.sin(2 * np.pi * freq * 2 * t)
    env = np.exp(-decay * t) if decay else np.ones_like(t)
    fade = min(len(t), int(RATE * 0.005))  # 딸깍 소리 방지용 5ms 페이드
    env[:fade] *= np.linspace(0, 1, fade)
    env[-fade:] *= np.linspace(1, 0, fade)
    return wave_ * env


def _silence(dur: float) -> np.ndarray:
    return np.zeros(int(RATE * dur))


def synth(kind: str) -> np.ndarray:
    if kind == "beep":
        return _tone(1200, 0.25)
    if kind == "buzzer":
        b = _tone(1000, 0.12, square=True)
        return np.concatenate([b, _silence(0.08), b])
    # chime: 딩(E6) - 동(C6), 종처럼 감쇠
    return np.concatenate([_tone(1318.5, 0.35, decay=6), _tone(1046.5, 0.6, decay=5)])


def wav_bytes(kind: str, volume: int) -> bytes:
    x = synth(kind)
    x = x / max(1e-9, np.abs(x).max()) * (max(0, min(100, volume)) / 100) * 0.9
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _file(kind: str, volume: int) -> Path:
    key = (kind if kind in SOUNDS else "chime", volume)
    with _lock:
        if key not in _cache:
            path = Path(tempfile.gettempdir()) / f"cctv_viewer_{key[0]}_{volume}.wav"
            path.write_bytes(wav_bytes(*key))
            _cache[key] = path
        return _cache[key]


def play(kind: str = "chime", volume: int = 80) -> None:
    """비동기 재생 (GUI 를 막지 않음). 재생 수단이 없으면 시스템 기본 비프."""
    try:
        path = _file(kind, volume)
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        for cmd in (["paplay", str(path)], ["pw-play", str(path)], ["aplay", "-q", str(path)]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    except Exception as e:  # 소리 문제로 감지가 멈추면 안 됨
        log.warning("효과음 재생 실패: %s", e)
    from PyQt6.QtWidgets import QApplication
    QApplication.beep()
