"""CCTV Viewer 진입점.  python main.py [--config path] [--debug]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# numpy 를 불러오기 전에 설정해야 한다. numpy 의 OpenBLAS 는 CPU 스레드 수만큼 작업 메모리를 미리 예약한다
# (8스레드 PC 실측: 개인 메모리 +235MB). 이 앱은 행렬 곱을 쓰지 않으므로 1스레드면 속도 차이 없음 (-> +10MB)
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from cctv_viewer import __version__
from cctv_viewer.config import default_config_path, is_portable, load_config
from cctv_viewer.main_window import MainWindow


def resource_path(rel: str) -> Path:
    """개발 실행과 PyInstaller 번들(sys._MEIPASS) 양쪽에서 리소스 경로 해석."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def selftest(out: Path) -> int:
    """배포본 점검: 디코더/하드웨어 가속/AI 모델 로드와 추론 1회."""
    import time
    import numpy as np
    lines = [f"CCTV Viewer {__version__}"]
    try:
        from cctv_viewer.stream import resolve_hwaccel
        lines.append(f"hwaccel auto: {resolve_hwaccel('auto')}")
        from cctv_viewer.detect import GROUPS, DetectionEngine, to_input
        for model in ("fast", "accurate"):
            eng = DetectionEngine(lambda *a: None, model=model)
            x = to_input(np.zeros((234, 416, 3), np.uint8))
            t = time.perf_counter()
            raw = eng._session.run(None, {eng._input: x})[0]
            lines.append(f"AI {model} ({eng.device_label}): OK output={raw.shape} {(time.perf_counter() - t) * 1000:.0f}ms")
            eng.stop()
        rc = 0
    except Exception as e:
        lines.append(f"FAIL: {type(e).__name__}: {e}")
        rc = 1
    out.write_text("\n".join(lines), encoding="utf-8")
    return rc


def update_now(config_path: Path) -> int:
    """관리용: 창 없이 업데이트. 결과는 config.json 옆 update.log 에 기록."""
    from cctv_viewer import updater
    log_path = config_path.parent / "update.log"
    try:
        cfg = load_config(config_path)
        info = updater.check(cfg.update_url)
        if info is None or not info.newer:
            msg = f"최신 버전입니다 (v{__version__})"
        else:
            path = updater.download(info, updater.download_dir(info.kind), lambda f: None)
            updater.apply(info, path)
            msg = f"v{__version__} -> v{info.version} 설치 시작"
        rc = 0
    except Exception as e:
        msg, rc = f"실패: {e}", 1
    log_path.write_text(msg, encoding="utf-8")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Hikvision 초경량 RTSP 뷰어")
    ap.add_argument("--config", type=Path, default=default_config_path())
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--selftest", metavar="OUT", help="하드웨어 디코딩/AI 엔진 점검 결과를 파일로 쓰고 종료")
    ap.add_argument("--cleanup", metavar="OLD_EXE", help=argparse.SUPPRESS)  # 포터블 업데이트 후 이전 exe 삭제
    ap.add_argument("--update-now", action="store_true", help="창 없이 최신 버전 확인/설치 (관리용)")
    args = ap.parse_args()
    if args.selftest:
        return selftest(Path(args.selftest))
    if args.update_now:
        return update_now(args.config)
    if args.cleanup:
        import threading
        from cctv_viewer.updater import cleanup_old
        threading.Thread(target=cleanup_old, args=(args.cleanup,), daemon=True).start()

    # 콘솔 + 파일 로그 (config.json 옆 cctv_viewer.log, 1MB x 3개 순환)
    from logging.handlers import RotatingFileHandler
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(args.config.parent / "cctv_viewer.log", maxBytes=1_000_000,
                                            backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # PyQt6 는 슬롯에서 처리되지 않은 예외가 나면 앱 전체를 강제 종료(qFatal)한다.
    # 사용자 excepthook 이 있으면 종료하지 않으므로, 기록만 하고 계속 동작하게 한다 (24시간 모니터링용).
    sys.excepthook = lambda t, v, tb: logging.getLogger("uncaught").error("처리되지 않은 오류", exc_info=(t, v, tb))
    # PyAV 로그 콜백은 FFmpeg 스레드에서 GIL 을 잡아 멀티스트림에서 지연/교착을 유발할 수
    # 있으므로 디버그 모드에서만 활성화 (기본값: FFmpeg 로그를 Python 으로 넘기지 않음)
    if args.debug:
        import av.logging
        av.logging.set_level(av.logging.VERBOSE)

    app = QApplication(sys.argv)
    app.setApplicationName("CCTV Viewer")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(resource_path("assets/cctv.ico"))))
    win = MainWindow(load_config(args.config), args.config)
    edition = "포터블" if is_portable() else ""
    win.setWindowTitle(f"CCTV Viewer v{__version__} {edition}".rstrip())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
