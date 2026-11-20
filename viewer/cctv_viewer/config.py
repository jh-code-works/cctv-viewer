"""config.json 로드/저장 및 카메라별(Hikvision / Tapo) RTSP URL 생성."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

MAX_CAMERAS = 6

MAIN = "main"
SUB = "sub"

# 화면 배열: 이름 -> (가로 칸 수, 세로 칸 수)
LAYOUTS = {"2x2": (2, 2), "3x2": (3, 2), "3x3": (3, 3)}

# Hikvision stream type suffix: /Streaming/Channels/{channel}0{type}
_HIK_STREAM_TYPE = {MAIN: 1, SUB: 2}
# TP-Link Tapo: /stream1 = 메인(고화질), /stream2 = 서브(저화질). 채널 개념 없음
_TAPO_STREAM = {MAIN: "stream1", SUB: "stream2"}

HIKVISION = "hikvision"
TAPO = "tapo"
VENDORS = {HIKVISION: "Hikvision", TAPO: "Tapo"}


@dataclass
class Camera:
    name: str = ""
    enabled: bool = False
    ip: str = ""
    port: int = 554
    username: str = "admin"
    password: str = ""
    channel: int = 1  # 단독 IP 카메라는 1, NVR 경유 시 NVR 채널 번호
    ai_mask: str = ""  # 감지 제외 영역 (펜으로 칠한 곳, mask.py 형식)
    vendor: str = HIKVISION  # hikvision | tapo (Tapo 는 앱에서 만든 '카메라 계정'으로 접속)

    def rtsp_url(self, stream: str) -> str:
        """Hikvision: rtsp://user:pass@ip:port/Streaming/Channels/{channel}0{1|2}
        Tapo:      rtsp://user:pass@ip:port/stream{1|2}"""
        if self.vendor == TAPO:
            path = f"/{_TAPO_STREAM[stream]}"
        else:
            path = f"/Streaming/Channels/{self.channel}0{_HIK_STREAM_TYPE[stream]}"
        # 비밀번호에 @ : / # 등이 있어도 URL이 깨지지 않도록 인코딩
        cred = ""
        if self.username:
            cred = quote(self.username, safe="")
            if self.password:
                cred += ":" + quote(self.password, safe="")
            cred += "@"
        return f"rtsp://{cred}{self.ip}:{self.port}{path}"

    def playback_url(self, start) -> str:
        """NVR 녹화 재생: /Streaming/tracks/{channel}01?starttime=YYYYMMDDTHHMMSSZ
        Hikvision 은 'Z' 를 붙여도 장비 현지 시각으로 해석한다 (실측: OSD 시각과 일치)."""
        live = self.rtsp_url(MAIN)
        base = live.replace(f"/Streaming/Channels/{self.channel}0", f"/Streaming/tracks/{self.channel}0")
        return f"{base}?starttime={start:%Y%m%dT%H%M%S}Z"


@dataclass
class Go2rtcSource:
    """go2rtc 릴레이 경유 수신. 활성화 시 카메라에 직접 붙지 않는다."""

    enabled: bool = False
    host: str = "192.168.0.10"
    port: int = 8554
    username: str = ""
    password: str = ""

    def rtsp_url(self, index: int, stream: str) -> str:
        cred = ""
        if self.username:
            cred = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        # go2rtc.yaml 의 스트림 이름 규칙: cam{N}_main / cam{N}_sub
        return f"rtsp://{cred}{self.host}:{self.port}/cam{index + 1}_{stream}"


@dataclass
class AppConfig:
    cameras: list[Camera] = field(default_factory=list)
    layout: str = "2x2"  # LAYOUTS 의 키
    # 해상도(스트림) 선택. 창 크기와 무관하게 고정. main = 메인스트림(고해상도), sub = 서브스트림(저해상도)
    grid_quality: str = MAIN        # 그리드(2x2/3x2/3x3) 화면
    fullscreen_quality: str = MAIN  # 더블클릭 확대 화면
    # 마지막으로 확인된 실제 해상도 (설정 화면 표시용) {"main": [w, h], "sub": [w, h]}
    seen_resolutions: dict = field(default_factory=dict)
    # auto | none | d3d11va | dxva2 | cuda | qsv
    hwaccel: str = "auto"
    # 하드웨어 디코딩을 적용할 스트림. 360p 서브스트림은 GPU->RAM 복사 비용이
    # 소프트웨어 디코딩보다 비싼 경우가 많아 기본값은 메인스트림에만 적용.
    hwaccel_on_sub: bool = False
    rtsp_transport: str = "tcp"  # tcp | udp
    grid_max_fps: int = 15
    fullscreen_max_fps: int = 30
    # 지터 버퍼 최대 지연(ms). 무선 카메라의 몰림 도착을 흡수해 부드럽게 재생.
    # 실제 지연은 측정된 흔들림만큼만 자동으로 잡힌다. 0 = 끄기 (받는 즉시 표시, 최저 지연)
    jitter_buffer_ms: int = 2000
    # ---- AI 감지 (사람/차량/동물) ----
    ai_enabled: bool = True
    ai_targets: list = field(default_factory=lambda: ["person", "vehicle"])  # person | vehicle | animal
    ai_confidence: int = 60      # 신뢰도 기준(%). 낮출수록 민감(오탐 증가), 높일수록 확실한 것만
    ai_fps: float = 3.0          # 카메라당 초당 분석 장수 (움직임 있을 때만 AI 실행)
    ai_model: str = "fast"       # fast = YOLOX-Nano / accurate = YOLOX-Tiny (멀리 있는 작은 대상에 유리)
    ai_device: str = "auto"      # auto = GPU(DirectML/CUDA) 우선 후 CPU 폴백 / cpu = CPU만 사용
    ai_sound: bool = True        # 새 이벤트(움직임 확정) 시 효과음 1회
    ai_sound_kind: str = "chime"  # chime(딩동) | beep(삐) | buzzer(삐삐)
    ai_sound_volume: int = 80     # 0~100
    ai_snapshots: bool = True    # 새 이벤트 스냅샷 저장 (config.json 옆 snapshots 폴더)
    ai_cooldown_s: int = 15
    # 제자리 물체(움직임 미확정) 박스 표시. 기본은 숨김: AI 가 드럼통·천막 등을 차량/사람으로 잘못 잡은 박스가
    # 화면에 남지 않도록 실제로 움직인 대상만 그린다 (주차된 차도 보고 싶으면 켬)
    ai_show_stationary: bool = False
    # 감지 시 자동 확대: 새 대상이 나타난 카메라를 확대 화면 해상도(기본 메인)로 몇 초 보여주고 원래 화면으로 복귀
    auto_zoom: bool = True
    auto_zoom_seconds: int = 3
    auto_zoom_targets: list = field(default_factory=lambda: ["person", "vehicle"])
    # 업데이트 서버 URL (예: https://example.com/cctv/). 설정 -> [업데이트 확인]
    update_url: str = ""
    go2rtc: Go2rtcSource = field(default_factory=Go2rtcSource)

    def __post_init__(self) -> None:
        while len(self.cameras) < MAX_CAMERAS:
            n = len(self.cameras) + 1
            self.cameras.append(Camera(name=f"CAM {n}"))
        del self.cameras[MAX_CAMERAS:]

    def stream_url(self, index: int, stream: str) -> str:
        if self.go2rtc.enabled:
            return self.go2rtc.rtsp_url(index, stream)
        return self.cameras[index].rtsp_url(stream)

    def enabled_indices(self) -> list[int]:
        return [i for i, c in enumerate(self.cameras) if c.enabled and (c.ip or self.go2rtc.enabled)]

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        cams = [Camera(**_known(Camera, c)) for c in data.get("cameras", [])]
        go2rtc = Go2rtcSource(**_known(Go2rtcSource, data.get("go2rtc", {})))
        rest = {k: v for k, v in _known(cls, data).items() if k not in ("cameras", "go2rtc")}
        layout = rest.get("layout")  # 구버전(v1.0.18 까지)은 숫자 2/3 = 2x2/3x3
        rest["layout"] = f"{layout}x{layout}" if layout in (2, 3) else layout if layout in LAYOUTS else "2x2"
        for key in ("grid_quality", "fullscreen_quality"):  # 구버전 "auto" 값은 메인으로
            if rest.get(key) not in (MAIN, SUB):
                rest.pop(key, None)
        return cls(cameras=cams, go2rtc=go2rtc, **rest)

    def to_dict(self) -> dict:
        return asdict(self)


def _known(klass, data: dict) -> dict:
    """알 수 없는 키는 무시해 구버전/신버전 config.json 호환성 유지."""
    names = klass.__dataclass_fields__.keys()
    return {k: v for k, v in data.items() if k in names}


def is_portable() -> bool:
    """포터블 빌드는 번들 안에 portable.flag 를 넣어 구분한다 (build_exe.py)."""
    bundle = getattr(sys, "_MEIPASS", None)
    return bool(bundle) and (Path(bundle) / "portable.flag").exists()


def default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        if is_portable():
            # 포터블: exe 옆에 저장 -> USB 등으로 설정째 들고 다닐 수 있음
            # (onefile 의 __file__ 은 임시 폴더라 sys.executable 기준)
            base = Path(sys.executable).resolve().parent
        elif sys.platform == "win32":
            # 설치형: Program Files 는 쓰기 불가 -> 사용자별 %APPDATA%\CCTVViewer
            base = Path(os.environ.get("APPDATA", Path.home())) / "CCTVViewer"
            base.mkdir(parents=True, exist_ok=True)
        else:
            # 리눅스(.deb, /opt 설치): XDG 규약 ~/.config/cctv-viewer
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cctv-viewer"
            base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "config.json"


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg
    with path.open("r", encoding="utf-8") as f:
        return AppConfig.from_dict(json.load(f))


def save_config(cfg: AppConfig, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 저장 도중 종료돼도 기존 파일이 깨지지 않도록 원자적 교체
