"""카메라별 '감지 제외 영역' 마스크 (사용자가 펜으로 칠한 곳).

- 128x72 격자(16:9)의 True/False. 화면 전체를 0~1 정규화 좌표로 덮으므로 해상도(메인/서브)와 무관.
- config.json 에는 비트를 압축한 base64 문자열로 저장 (카메라당 약 1.5KB).
- 제외 규칙: 물체의 "발 위치"(박스 아래 중앙)가 칠한 곳에 있거나, 박스 면적의 85% 이상이 칠한 곳이면 제외.
  -> 오두막을 칠하면 오두막 오탐은 사라지고, 오두막 앞을 지나가는 사람(발은 칠한 곳 밖)은 계속 감지.
"""
from __future__ import annotations

import base64

import numpy as np

W, H = 128, 72


def empty() -> np.ndarray:
    return np.zeros((H, W), bool)


def encode(mask: np.ndarray) -> str:
    if mask is None or not mask.any():
        return ""
    return base64.b64encode(np.packbits(mask.astype(bool)).tobytes()).decode()


def decode(text: str) -> np.ndarray | None:
    if not text:
        return None
    try:
        bits = np.unpackbits(np.frombuffer(base64.b64decode(text), np.uint8))[: W * H]
        m = bits.reshape(H, W).astype(bool)
        return m if m.any() else None
    except Exception:
        return None  # 손상된 값은 무시 (감지는 계속)


def resized(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """최근접 이웃으로 다른 격자 크기에 맞춤 (움직임 판별용 축소 이미지 등)."""
    h, w = shape
    ys = (np.arange(h) * H // max(1, h)).clip(0, H - 1)
    xs = (np.arange(w) * W // max(1, w)).clip(0, W - 1)
    return mask[np.ix_(ys, xs)]


def excluded(mask: np.ndarray, box: tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = box
    fx = min(W - 1, max(0, int((x1 + x2) / 2 * W)))
    fy = min(H - 1, max(0, int(y2 * H - 1e-6)))
    if mask[fy, fx]:
        return True  # 발 위치가 제외 영역
    c1, c2 = max(0, int(x1 * W)), min(W, max(int(x1 * W) + 1, int(np.ceil(x2 * W))))
    r1, r2 = max(0, int(y1 * H)), min(H, max(int(y1 * H) + 1, int(np.ceil(y2 * H))))
    region = mask[r1:r2, c1:c2]
    return region.size > 0 and region.mean() >= 0.85  # 거의 전부 칠한 곳 안 (몸 일부만 겹친 행인은 유지)


def filter_dets(mask: np.ndarray | None, dets: list) -> list:
    if mask is None:
        return dets
    return [d for d in dets if not excluded(mask, d.box)]
