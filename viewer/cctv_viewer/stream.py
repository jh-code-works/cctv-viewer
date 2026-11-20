"""PyAV 기반 저지연 RTSP 디코딩 워커.

설계 원칙 (go2rtc / Frigate / PyQt-RTSP 패턴 참고)
- 카메라당 디코딩 스레드 1개. GUI 스레드는 디코딩/색변환을 하지 않는다.
- 버퍼 크기 1 (latest-frame slot): GUI가 직전 프레임을 아직 가져가지 않았으면
  새 프레임은 색변환 없이 버린다. 디코딩은 H.264/H.265 참조 구조상 모든 프레임을
  해야 하지만, 가장 비싼 색변환+스케일(swscale)은 실제 화면에 그릴 프레임에만 수행.
- 색변환은 위젯 크기로 바로 축소하며 수행 (4K -> 셀 크기). Qt에서 다시 스케일하지 않음.
- 적응형 지터 버퍼: 무선(Wi-Fi) 카메라처럼 패킷이 몰려서 도착하는 환경에서 받는 즉시
  그리면 끊겨 보인다. 압축 패킷을 측정된 흔들림만큼만 잠시 모았다가 카메라 PTS 시간표대로
  재생한다 (WebRTC/ffplay 방식). 패킷 단위라 메모리는 채널당 수백 KB, CPU 추가 부담 없음.
- 디코딩 적체 감지: 버퍼에 쌓인 분량이 한도를 넘으면 다음 키프레임까지 버리고 실시간 복귀.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections import deque
from typing import Callable

import av
from av.codec.hwaccel import HWAccel, hwdevices_available
from av.video.reformatter import VideoReformatter
from PyQt6.QtGui import QImage

log = logging.getLogger(__name__)

# 버퍼에 쌓인 분량이 (지터 버퍼 최대 + 이 값)초를 넘으면 다음 키프레임까지 스킵.
# 순간적인 무선 지연(측정상 최대 ~1.2초)으로는 발동하지 않도록 넉넉히 둔다.
LAG_SKIP_MARGIN = 2.0
MIN_JITTER_DELAY = 0.08  # 지터 버퍼 최소 지연(초)
RECONNECT_MAX_BACKOFF = 10.0

if sys.platform == "win32":
    _AUTO_ORDER = ("d3d11va", "dxva2", "cuda", "qsv")
elif sys.platform == "darwin":
    _AUTO_ORDER = ("videotoolbox",)
else:
    _AUTO_ORDER = ("vaapi", "cuda", "qsv")

_hw_cache: dict[str, str | None] = {}
_hw_lock = threading.Lock()


def _probe_device(device_type: str) -> bool:
    """디바이스 컨텍스트를 실제로 만들어 보고 사용 가능 여부 판단."""
    if device_type not in hwdevices_available():
        return False
    try:
        HWAccel(device_type, allow_software_fallback=False).create(av.Codec("h264", "r"))
        return True
    except Exception as e:  # 드라이버 없음, GPU 없음 등
        log.debug("hwaccel %s unavailable: %s", device_type, e)
        return False


def resolve_hwaccel(name: str) -> str | None:
    """'auto' 이면 사용 가능한 첫 디바이스, 'none' 이면 None, 그 외는 검증 후 반환."""
    name = (name or "none").lower()
    if name == "none":
        return None
    with _hw_lock:
        if name not in _hw_cache:
            candidates = _AUTO_ORDER if name == "auto" else (name,)
            _hw_cache[name] = next((d for d in candidates if _probe_device(d)), None)
            log.info("hwaccel %s -> %s", name, _hw_cache[name])
        return _hw_cache[name]


class JitterBuffer:
    """압축 패킷용 적응형 지터 버퍼.

    도착시각 - PTS 의 최솟값(= 가장 빨리 도착한 경우)을 기준선으로 삼고, 최근 패킷들의
    지연 편차 최대값만큼 재생을 늦춘다. 흔들림이 커지면 즉시 늘리고, 줄어들면 천천히 줄인다.
    기준선은 10초 창 2개의 최솟값이라 카메라/PC 시계 차이(drift)도 따라간다.
    """

    FLUSH = object()

    def __init__(self, max_delay: float) -> None:
        self.max_delay = max(0.0, max_delay)
        self.delay = min(MIN_JITTER_DELAY * 2, self.max_delay)
        self._q: deque = deque()  # (due, pts, packet)
        self._cv = threading.Condition()
        self._recent: deque = deque(maxlen=200)
        self._reset_clock(math.inf, time.monotonic())

    def _reset_clock(self, off: float, now: float) -> None:
        self._win_min = self._prev_min = off
        self._win_t0 = now
        self._recent.clear()

    def push(self, packet, pts: float | None) -> None:
        now = time.monotonic()
        if pts is None or self.max_delay <= 0:
            due = now
        else:
            off = now - pts
            base = min(self._win_min, self._prev_min)
            if base != math.inf and abs(off - base) > 5.0:
                # PTS 불연속 (NVR 재연결, 타임스탬프 리셋) -> 추정치 초기화
                self._reset_clock(off, now)
            if now - self._win_t0 > 10.0:
                self._prev_min, self._win_min, self._win_t0 = self._win_min, math.inf, now
            self._win_min = min(self._win_min, off)
            base = min(self._win_min, self._prev_min)
            self._recent.append(off - base)
            target = min(self.max_delay, max(MIN_JITTER_DELAY, max(self._recent) + 0.03))
            # 늘릴 때는 즉시, 줄일 때는 천천히 (줄였다가 다시 끊기는 것 방지)
            self.delay = target if target > self.delay else self.delay + (target - self.delay) * 0.01
            due = pts + base + self.delay
        with self._cv:
            self._q.append((due, pts, packet))
            self._cv.notify()

    def flush(self) -> None:
        """대기 패킷을 모두 버리고 디코더 초기화를 요청 (키프레임 스킵 시)."""
        with self._cv:
            self._q.clear()
            self._q.append((0.0, None, self.FLUSH))
            self._cv.notify()

    def buffered_seconds(self) -> float:
        with self._cv:
            if len(self._q) < 2:
                return 0.0
            first, last = self._q[0][1], self._q[-1][1]
        return last - first if first is not None and last is not None else 0.0

    def pop(self, stop: threading.Event):
        """재생 시각이 된 패킷을 반환. 정지 시 None."""
        with self._cv:
            while not stop.is_set():
                if self._q:
                    wait = self._q[0][0] - time.monotonic()
                    if wait <= 0:
                        return self._q.popleft()[2]
                    self._cv.wait(min(wait, 0.1))
                else:
                    self._cv.wait(0.1)
        return None

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()


class StreamWorker(threading.Thread):
    """RTSP 1개를 수신/디코딩해 최신 프레임 1장만 보관하는 스레드.

    on_frame(): 새 프레임이 준비됐을 때 호출 (워커 스레드에서 호출됨 -> Qt 시그널 emit 용)
    on_status(text): 연결 상태 변경 시 호출
    """

    def __init__(
        self,
        url: str,
        *,
        hwaccel: str | None,
        transport: str = "tcp",
        max_fps: int = 15,
        decode_threads: int = 1,
        jitter_max: float = 1.0,
        detector=None,
        cam_id: int = -1,
        on_frame: Callable[[], None],
        on_status: Callable[[str], None],
    ) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.hwaccel = hwaccel
        self.transport = transport
        self.min_interval = 1.0 / max(1, max_fps)
        self.decode_threads = decode_threads
        self.jitter_max = jitter_max
        self.detector = detector  # detect.DetectionEngine | None (GUI 에서 켜고 끌 수 있음)
        self.cam_id = cam_id
        self._on_frame = on_frame
        self._on_status = on_status

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._latest: QImage | None = None
        self._consumed = True
        self._box = (640, 360)  # 출력 영역 (물리 픽셀)

        # 통계 (GUI 오버레이용, 대략적인 값이면 충분하므로 락 없이 갱신)
        self.src_size = (0, 0)
        self.decoder_name = ""
        self.render_fps = 0.0
        self.buffer_ms = 0.0
        # swscale 변환기를 프레임마다 새로 만들면 필터 테이블 초기화에 12~15ms 가 든다(1080p 실측).
        # 재사용하면 1~2ms. 재생 스레드에서만 사용.
        self._reformatter = VideoReformatter()
        self._det_reformatter = VideoReformatter()  # AI 입력(416 레터박스, RGB)용

    # ---- GUI 스레드에서 호출 -------------------------------------------------
    def set_box(self, width: int, height: int) -> None:
        self._box = (max(16, width), max(16, height))

    def take_frame(self) -> QImage | None:
        """최신 프레임을 가져가며 슬롯을 비운다 (buffer size = 1)."""
        with self._lock:
            frame, self._latest = self._latest, None
            self._consumed = True
            return frame

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def stopped(self) -> bool:
        return self._stop_evt.is_set()

    # ---- 워커 스레드 ---------------------------------------------------------
    def run(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            self._on_status("연결 중…")
            try:
                got_frame = self._run_once()
                if got_frame:
                    backoff = 1.0
                if self._stop_evt.is_set():
                    break
                self._on_status("스트림 종료, 재연결 대기")
            except Exception as e:  # av.error.* 포함, 어떤 오류든 재연결로 복구
                if self._stop_evt.is_set():
                    break
                msg = self._describe(e)
                log.warning("%s: %s", _mask(self.url), msg)
                if isinstance(e, av.error.HTTPUnauthorizedError):
                    # 재시도하지 않는다: Hikvision 은 로그인 실패가 누적되면 접속 IP 를
                    # 일정 시간 잠근다(불법 로그인 잠금). 설정을 저장하면 워커가 새로 시작된다.
                    self._on_status(f"{msg}\n설정에서 계정을 확인한 뒤 저장하세요")
                    return
                self._on_status(f"오류: {msg}")
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)

    def _open(self):
        options = {
            "rtsp_transport": self.transport,
            "rtsp_flags": "prefer_tcp" if self.transport == "tcp" else "",
            "fflags": "nobuffer+discardcorrupt",
            "flags": "low_delay",
            "max_delay": "300000",        # 300ms: 재정렬 버퍼 최소화
            "reorder_queue_size": "0",   # RTP 재정렬 큐 비활성화 (UDP 지연 제거)
            "analyzeduration": "1000000",  # 스트림 분석 1초 -> 빠른 첫 화면
            "probesize": "500000",
        }
        options = {k: v for k, v in options.items() if v}
        # is_hw_owned: 디코딩한 프레임을 GPU 에 두고, 화면/AI 용으로 변환(reformat)하는 프레임만 CPU 로 복사.
        # 기본값은 버려질 프레임까지 매 프레임 GPU->CPU 복사 (1080p 25fps x 4채널이면 복사가 재생 스레드 시간의 22%)
        hw = HWAccel(self.hwaccel, allow_software_fallback=True, is_hw_owned=True) if self.hwaccel else None
        return av.open(self.url, options=options, timeout=(10.0, 5.0), hwaccel=hw)

    def _run_once(self) -> bool:
        """수신 스레드(여기)는 패킷을 지터 버퍼에 넣기만 하고, 재생 스레드가 시간표대로
        꺼내 디코딩/표시한다. 컨테이너는 재생 스레드가 끝난 뒤에 닫는다."""
        with self._open() as container:
            stream = container.streams.video[0]
            ctx = stream.codec_context
            if self.hwaccel is None:
                # 서브스트림은 1스레드가 가장 저렴 (프레임 스레딩은 스레드 수만큼 지연 추가)
                stream.thread_type = "AUTO" if self.decode_threads != 1 else "SLICE"
                ctx.thread_count = self.decode_threads
            self.decoder_name = f"{ctx.name}" + (f" / {self.hwaccel}" if self.hwaccel else "")
            tb = float(stream.time_base) if stream.time_base else 0.0

            jb = JitterBuffer(self.jitter_max)
            done = threading.Event()
            state = {"got_frame": False, "error": None}
            player = threading.Thread(target=self._play, args=(ctx, jb, done, state), daemon=True)
            player.start()
            skipping = False
            try:
                for packet in container.demux(stream):
                    if self._stop_evt.is_set() or done.is_set():
                        break
                    if packet.size == 0:
                        continue
                    # 적체: 재생이 수신을 못 따라가 버퍼가 한도를 넘으면 키프레임까지 버림
                    if not skipping and jb.buffered_seconds() > self.jitter_max + LAG_SKIP_MARGIN:
                        log.info("%s: backlog %.1fs, skipping to next keyframe",
                                 _mask(self.url), jb.buffered_seconds())
                        skipping = True
                        jb.flush()
                    if skipping:
                        if not packet.is_keyframe:
                            continue
                        skipping = False
                    jb.push(packet, packet.pts * tb if packet.pts is not None and tb else None)
                    self.buffer_ms = jb.delay * 1000
            finally:
                done.set()
                jb.wake()
                player.join(timeout=3.0)
            if state["error"] is not None:
                raise state["error"]
            return state["got_frame"]

    def _play(self, ctx, jb: JitterBuffer, done: threading.Event, state: dict) -> None:
        """재생 스레드: 재생 시각이 된 패킷을 디코딩하고, 그릴 프레임만 변환해 GUI 에 넘긴다."""
        next_due = 0.0
        fps_count, fps_t0 = 0, time.monotonic()
        try:
            while True:
                packet = jb.pop(done)
                if packet is None:
                    return
                if packet is JitterBuffer.FLUSH:
                    ctx.flush_buffers()
                    continue
                for frame in ctx.decode(packet):
                    if not state["got_frame"]:
                        state["got_frame"] = True
                        self.src_size = (frame.width, frame.height)
                        self._on_status("")
                    self._feed_detector(frame)
                    now = time.monotonic()
                    # 프레임 드롭: FPS 상한 초과 또는 GUI가 이전 프레임을 아직 안 가져감
                    if now < next_due or not self._consumed:
                        continue
                    # 누적 스케줄: 25fps 소스에서 15fps 상한이 12.5fps 로 떨어지는 양자화 방지
                    next_due = max(next_due + self.min_interval, now - self.min_interval)
                    img = self._convert(frame)
                    with self._lock:
                        self._latest = img
                        self._consumed = False
                    self._on_frame()

                    fps_count += 1
                    if now - fps_t0 >= 1.0:
                        self.render_fps = fps_count / (now - fps_t0)
                        fps_count, fps_t0 = 0, now
        except Exception as e:  # 디코딩 오류(스트림 암호화 등)는 수신 스레드로 넘겨 재연결 처리
            state["error"] = e
            done.set()

    def _feed_detector(self, frame: av.VideoFrame) -> None:
        """AI 감지 엔진에 초당 몇 장만 넘긴다 (416 크기로 줄인 RGB, 변환 ~0.5ms)."""
        det = self.detector
        if det is None or not det.wants_frame(self.cam_id):
            return
        from .detect import letterbox_size
        w, h, scale = letterbox_size(frame.width, frame.height)
        rgb = self._det_reformatter.reformat(frame, width=w, height=h, format="rgb24",
                                            interpolation="AREA").to_ndarray()
        det.submit(self.cam_id, rgb, frame.width, frame.height, scale)

    def _convert(self, frame: av.VideoFrame) -> QImage:
        """출력 영역에 맞게 종횡비 유지 축소 + BGRA 변환을 swscale 한 번으로 처리."""
        bw, bh = self._box
        sw, sh = frame.width, frame.height
        scale = min(bw / sw, bh / sh)
        w = max(2, int(sw * scale) & ~1)
        h = max(2, int(sh * scale) & ~1)
        # 스케일러: 크게 줄일 때는 AREA(에일리어싱 없음), 그 외(소폭 축소/확대)는 BICUBIC(선명).
        # FAST_BILINEAR 대비 프레임당 +0.5ms 수준 (1080p 기준 측정) -> CPU 영향 미미
        interp = "AREA" if scale < 0.5 else "BICUBIC"
        # BGRA (little-endian) == QImage.Format_RGB32 메모리 배치 -> 픽셀 변환 없음
        out = self._reformatter.reformat(frame, width=w, height=h, format="bgra", interpolation=interp)
        plane = out.planes[0]
        # FFmpeg 는 줄마다 정렬 패딩을 붙이므로(line_size > w*4) numpy 로 바꾸면 비연속 배열이 되어
        # QImage 가 받지 못한다. 평면 버퍼를 line_size 와 함께 그대로 넘긴다.
        # .copy() 로 Qt 소유 메모리로 옮긴다: 외부 버퍼를 물린 QImage 는 페인트 엔진이 얕은 복사본을
        # 쥐고 있는 동안 버퍼가 해제되면 크래시가 난다. 복사는 워커 스레드에서 수행.
        return QImage(memoryview(plane), w, h, plane.line_size, QImage.Format.Format_RGB32).copy()

    def _describe(self, e: Exception) -> str:
        # FFmpeg 오류 문자열에는 비밀번호가 포함된 URL 이 그대로 들어있으므로 반드시 제거
        # str(e) 에는 URL 이 들어 있어 숫자 부분일치(예: Channels/401)로 판별하면 오판하므로 타입으로만 판별
        if isinstance(e, av.error.HTTPUnauthorizedError):
            return "인증 실패 (계정/비밀번호 확인)"
        if isinstance(e, (av.error.TimeoutError, av.error.ExitError, TimeoutError)):
            # ExitError: PyAV timeout 인터럽트로 연결 시도가 중단된 경우
            return "응답 없음 (IP/포트/네트워크 확인)"
        if isinstance(e, av.error.HTTPNotFoundError):
            return "스트림 없음 (채널 번호/스트림 이름 확인)"
        if isinstance(e, (av.error.PatchWelcomeError, av.error.InvalidDataError)):
            # 접속/로그인은 됐는데 비트스트림 해석 불가. Hikvision 에서는 대부분
            # Hik-Connect '스트림 암호화' 로 VPS/SPS 까지 암호화된 경우 (외부 디코더로 해독 불가)
            return "영상 해독 불가: NVR/카메라의 '스트림 암호화'를 끄세요"
        if isinstance(e, ConnectionRefusedError) or getattr(e, "errno", None) in (10061, 111):
            return "연결 거부 (포트 확인: RTSP 는 보통 554)"
        s = getattr(e, "strerror", None) or str(e) or type(e).__name__
        s = s.replace(self.url, _mask(self.url))
        return s if len(s) < 60 else s[:57] + "…"


def _mask(url: str) -> str:
    """로그에 비밀번호가 남지 않도록 마스킹."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        cred, host = rest.rsplit("@", 1)
        user = cred.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url
