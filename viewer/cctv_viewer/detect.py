"""AI 객체 인식 (사람/차량/동물) — YOLOX + ONNX Runtime (GPU DirectML/CUDA 우선, CPU 자동 폴백).

부하 절감 설계
- 카메라마다 초당 몇 장(detect_fps)만 분석. 디코딩은 이미 뷰어가 하고 있으므로 추가 디코딩 없음.
- 1단계 움직임 판별: 64x36 흑백 축소 이미지의 프레임 차이만 계산 (~0.05ms).
  움직임이 없으면 AI 를 돌리지 않는다. 대상이 감지된 뒤 몇 초 동안은 움직임과 무관하게 계속 추적.
- AI 엔진 1개(스레드 1개)를 모든 카메라가 공유. 카메라별 대기열 크기 1 (최신 프레임만).

모델: YOLOX-Nano(빠름, 기본) / YOLOX-Tiny(정확) — Megvii, Apache-2.0, COCO 80 클래스, 입력 416x416.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

INPUT_SIZE = 416
MODELS = {"fast": "yolox_nano.onnx", "accurate": "yolox_tiny.onnx"}

# COCO 클래스 id -> 그룹
GROUPS: dict[str, dict[int, str]] = {
    "person": {0: "사람"},
    "vehicle": {1: "자전거", 2: "차량", 3: "오토바이", 5: "버스", 7: "트럭"},
    "animal": {14: "새", 15: "고양이", 16: "개"},
}
GROUP_COLORS = {"person": (255, 60, 60), "vehicle": (60, 170, 255), "animal": (255, 200, 40)}
_CLASS_TO_GROUP = {cid: g for g, m in GROUPS.items() for cid in m}
_CLASS_NAME = {cid: name for m in GROUPS.values() for cid, name in m.items()}


@dataclass
class Detection:
    group: str          # person | vehicle | animal
    label: str          # 표시 이름 (사람, 차량 …)
    score: float
    box: tuple[float, float, float, float]  # 원본 프레임 기준 정규화 좌표 x1, y1, x2, y2 (0~1)
    stationary: bool = False  # 같은 자리에 오래 있는 물체 (주차된 차 등) -> 이벤트/강조 제외


def iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def model_path(kind: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / "models" / MODELS.get(kind, MODELS["fast"])


# ---- 전처리/후처리 -------------------------------------------------------------
def _grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    grids, strides = [], []
    for stride in (8, 16, 32):
        n = size // stride
        yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((n * n, 1), stride))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


_GRID, _STRIDE = _grids(INPUT_SIZE)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


def postprocess(raw: np.ndarray, scale: float, src_w: int, src_h: int,
                classes: set[int], conf: float) -> list[Detection]:
    """YOLOX 원시 출력 (1, N, 85) -> Detection 목록."""
    out = raw[0]
    xy = (out[:, :2] + _GRID) * _STRIDE
    wh = np.exp(out[:, 2:4]) * _STRIDE
    scores_all = out[:, 4:5] * out[:, 5:]
    cls = scores_all.argmax(1)
    score = scores_all[np.arange(len(cls)), cls]
    mask = (score >= conf) & np.isin(cls, list(classes))
    if not mask.any():
        return []
    xy, wh, cls, score = xy[mask], wh[mask], cls[mask], score[mask]
    boxes = np.concatenate((xy - wh / 2, xy + wh / 2), 1) / scale  # 레터박스 해제 -> 원본 픽셀
    result: list[Detection] = []
    for c in np.unique(cls):  # 클래스별 NMS
        idx = np.where(cls == c)[0]
        for k in _nms(boxes[idx], score[idx], 0.45):
            x1, y1, x2, y2 = boxes[idx[k]]
            result.append(Detection(
                _CLASS_TO_GROUP[int(c)], _CLASS_NAME[int(c)], float(score[idx[k]]),
                (max(0.0, x1 / src_w), max(0.0, y1 / src_h), min(1.0, x2 / src_w), min(1.0, y2 / src_h))))
    return result


def letterbox_size(src_w: int, src_h: int) -> tuple[int, int, float]:
    scale = min(INPUT_SIZE / src_w, INPUT_SIZE / src_h)
    return max(2, int(src_w * scale) & ~1), max(2, int(src_h * scale) & ~1), scale


def to_input(rgb: np.ndarray) -> np.ndarray:
    """(h, w, 3) RGB uint8 (h, w <= 416) -> (1, 3, 416, 416) float32. YOLOX: BGR, 0~255, 좌상단 정렬 패딩 114."""
    h, w = rgb.shape[:2]
    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, np.uint8)
    canvas[:h, :w] = rgb[..., ::-1]
    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)


# ---- 엔진 ----------------------------------------------------------------------
class _CamState:
    __slots__ = ("pending", "prev_small", "active_until", "last_run", "last_score")

    def __init__(self) -> None:
        self.pending = None  # (rgb, src_w, src_h, scale, t)
        self.prev_small: np.ndarray | None = None
        self.active_until = 0.0
        self.last_run = 0.0
        self.last_score = 0.0


_PROVIDER_LABELS = {
    "DmlExecutionProvider": "GPU (DirectML)",
    "CUDAExecutionProvider": "GPU (CUDA)",
    "CPUExecutionProvider": "CPU",
}


def _session_options(ort, *, use_gpu: bool):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2  # 뷰어 디코딩 스레드와 경쟁하지 않도록 제한
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # DirectML 은 메모리 패턴 최적화를 꺼야 안정적이며, CPU 모드에서는 spin 비활성화로 유휴 CPU 사용을 줄인다.
    if use_gpu:
        opts.enable_mem_pattern = False
    else:
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return opts


def _create_session(ort, path: str, device: str):
    """요청된 장치(auto | cpu)에 맞는 ONNX Runtime 세션을 생성한다. GPU 실패 시 CPU 로 폴백."""
    if device != "cpu":
        avail = set(ort.get_available_providers())
        gpu_providers = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider") if p in avail]
        for prov in gpu_providers:
            try:
                opts = _session_options(ort, use_gpu=True)
                sess = ort.InferenceSession(path, opts, providers=[prov, "CPUExecutionProvider"])
                return sess
            except Exception as e:
                log.warning("AI GPU(%s) 초기화 실패, 다음 장치로 폴백: %s", prov, e)
    return ort.InferenceSession(path, _session_options(ort, use_gpu=False), providers=["CPUExecutionProvider"])


class DetectionEngine:
    """모든 카메라가 공유하는 AI 스레드 1개.

    submit(): 재생 스레드에서 호출. 움직임이 없으면 즉시 버리고, 있으면 최신 프레임만 대기열에 둔다.
    on_result(cam, detections, ts): 엔진 스레드에서 호출 (GUI 로는 Qt 시그널로 넘길 것).
    """

    MOTION_PERCENT = 0.5     # 64x36 흑백 축소본에서 밝기가 크게 바뀐 칸의 비율(%)이 이 이상이면 '움직임'
    TRACK_HOLD = 5.0         # hold() 후 이 시간(초) 동안은 움직임 없어도 계속 분석

    def __init__(self, on_result: Callable[[int, list[Detection], float], None],
                 model: str = "fast", device: str = "auto") -> None:
        import onnxruntime as ort  # 선택 기능: 감지를 켤 때만 로드

        self.model = model
        self.device = device
        self._session = _create_session(ort, str(model_path(model)), device)
        active = self._session.get_providers()
        self.provider = active[0] if active else "CPUExecutionProvider"
        self.device_label = _PROVIDER_LABELS.get(self.provider, self.provider)
        log.info("AI 엔진 로드: model=%s device=%s (%s)", model, self.device_label, self.provider)
        self._input = self._session.get_inputs()[0].name
        self._on_result = on_result
        self._cams: dict[int, _CamState] = {}
        self._lock = threading.Lock()
        self._evt = threading.Event()
        self._stop = threading.Event()
        self.classes: set[int] = set(GROUPS["person"])
        self.conf = 0.5
        self.fps = 3.0
        self.infer_ms = 0.0
        self._masks: dict = {}
        self.runs = 0           # AI 실행 횟수
        self.motion_skips = 0   # 움직임이 없어 AI 를 건너뛴 횟수
        self._thread = threading.Thread(target=self._run, name="ai-detect", daemon=True)
        self._thread.start()

    def set_mask(self, cam: int, mask) -> None:
        """감지 제외 영역 (numpy bool 72x128 | None). 칠한 곳의 움직임(흔들리는 나무 등)으로는 AI 를 돌리지 않는다."""
        if mask is None:
            self._masks.pop(cam, None)
        else:
            self._masks[cam] = mask

    def configure(self, groups: list[str], conf: float, fps: float) -> None:
        self.classes = {cid for g in groups for cid in GROUPS.get(g, {})}
        self.conf = conf
        self.fps = max(0.5, fps)

    def wants_frame(self, cam: int) -> bool:
        """이번 프레임을 보낼 차례인가 (카메라별 detect_fps 제한). 재생 스레드에서 호출."""
        st = self._cams.get(cam)
        return st is None or time.monotonic() - st.last_run >= 1.0 / self.fps

    def submit(self, cam: int, rgb: np.ndarray, src_w: int, src_h: int, scale: float) -> None:
        now = time.monotonic()
        with self._lock:
            st = self._cams.setdefault(cam, _CamState())
            st.last_run = now
        # 움직임 판별 (64x36 흑백). 대상 추적 중에는 생략
        small = rgb[::max(1, rgb.shape[0] // 36), ::max(1, rgb.shape[1] // 64)].mean(2).astype(np.int16)
        moving = True
        if st.prev_small is not None and st.prev_small.shape == small.shape and now > st.active_until:
            diff = np.abs(small - st.prev_small) > 18
            m = self._masks.get(cam)
            if m is not None:
                from .mask import resized
                keep = ~resized(m, diff.shape)
                diff = diff[keep] if keep.any() else diff[:0]
            changed = diff.mean() * 100 if diff.size else 0.0
            st.last_score = changed
            moving = changed >= self.MOTION_PERCENT
        st.prev_small = small
        if not moving:
            self.motion_skips += 1
            try:
                self._on_result(cam, [], now)  # 움직임 없음 -> 기존 박스 지우기
            except RuntimeError:  # 앱 종료 중
                pass
            return
        with self._lock:
            st.pending = (rgb, src_w, src_h, scale, now)
        self._evt.set()

    def hold(self, cam: int) -> None:
        """추적할 대상이 있음 -> TRACK_HOLD 동안 움직임과 무관하게 계속 분석 (GUI 스레드에서 호출).
        감지 결과가 있다고 무조건 연장하면 주차된 차/오탐 물체가 있는 카메라는 AI 가 쉬지 않는다
        (실측: 정지 물체 1개로 AI 실행의 2/3 가 연장분). 판단은 추적기(events.AiController)가 한다."""
        with self._lock:
            st = self._cams.get(cam)
            if st is not None:
                st.active_until = time.monotonic() + self.TRACK_HOLD

    def forget(self, cam: int) -> None:
        with self._lock:
            self._cams.pop(cam, None)

    def stop(self) -> None:
        self._stop.set()
        self._evt.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._evt.wait(0.5)
            self._evt.clear()
            while True:
                with self._lock:
                    job = next(((c, s.pending) for c, s in self._cams.items() if s.pending is not None), None)
                    if job is None:
                        break
                    cam, pending = job
                    self._cams[cam].pending = None
                rgb, src_w, src_h, scale, ts = pending
                try:
                    t = time.perf_counter()
                    raw = self._session.run(None, {self._input: to_input(rgb)})[0]
                    dets = postprocess(raw, scale, src_w, src_h, self.classes, self.conf)
                    self.infer_ms = (time.perf_counter() - t) * 1000
                    self.runs += 1
                except Exception as e:  # 모델 오류가 뷰어 전체를 멈추지 않도록
                    log.warning("detect error: %s", e)
                    continue
                try:
                    self._on_result(cam, dets, ts)
                except RuntimeError:  # 앱 종료 중 (GUI 객체가 이미 삭제됨)
                    return
