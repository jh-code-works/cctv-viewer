"""자동 업데이트: NAS(Web Station) 의 web/cctv 폴더에서 최신 버전을 받아 설치.

서버 구성 (build_exe.py 가 release/update/ 에 만들어 줌 -> NAS web/cctv 에 그대로 복사)
    latest.json       {"version": "1.0.10", "notes": "...", "files": {"windows-portable": {...}, ...}}
    latest.json.sig   latest.json 원본 바이트에 대한 Ed25519 서명 (base64)
    CCTVViewer_v1.0.10_Portable.exe / CCTVViewer_v1.0.10_Setup.exe / cctv-viewer_1.0.10_amd64.deb

보안
- HTTPS + 서명 검증: 서명 개인 키는 빌드 PC 에만 있고 앱에는 공개 키만 들어 있다.
  NAS 가 탈취돼 파일이 바뀌어도 서명이 맞지 않으면 설치하지 않는다.
- 각 파일은 latest.json 에 적힌 SHA-256 과 크기가 일치해야 설치한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_UPDATE_URL = ""
MANIFEST = "latest.json"
TIMEOUT = 15


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    notes: str
    kind: str          # windows-portable | windows-setup | linux-deb
    name: str
    sha256: str
    size: int
    url: str

    @property
    def newer(self) -> bool:
        return _vtuple(self.version) > _vtuple(__version__)


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def install_kind() -> str | None:
    """현재 실행 형태. 개발 실행(python main.py)은 None -> 확인만 가능."""
    if not getattr(sys, "frozen", False):
        return None
    from .config import is_portable
    if sys.platform == "win32":
        return "windows-portable" if is_portable() else "windows-setup"
    if sys.platform.startswith("linux"):
        return "linux-deb"
    return None


def _get(url: str) -> bytes:
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise UpdateError("업데이트 주소는 https 여야 합니다")
    req = urllib.request.Request(url, headers={"User-Agent": f"CCTVViewer/{__version__}",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def verify_signature(data: bytes, sig_b64: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from .update_key import PUBLIC_KEY
    if not PUBLIC_KEY:
        raise UpdateError("이 빌드에는 업데이트 공개 키가 없습니다")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY)).verify(
            base64.b64decode(sig_b64.strip()), data)
    except (InvalidSignature, ValueError):
        raise UpdateError("서명 검증 실패: 업데이트 파일이 변조됐거나 다른 키로 서명되었습니다") from None


def check(base_url: str = DEFAULT_UPDATE_URL) -> UpdateInfo | None:
    """최신 버전 정보. 이 설치 형태용 파일이 없으면 None."""
    if not base_url or not base_url.strip():
        raise UpdateError("업데이트 서버 URL이 설정되지 않았습니다")
    base = base_url.rstrip("/") + "/"
    try:
        raw = _get(base + MANIFEST)
        sig = _get(base + MANIFEST + ".sig")
    except urllib.error.HTTPError as e:
        raise UpdateError(f"업데이트 서버 응답 {e.code} ({base + MANIFEST})") from None
    except OSError as e:
        raise UpdateError(f"업데이트 서버에 연결할 수 없습니다 ({e.__class__.__name__})") from None
    verify_signature(raw, sig)
    m = json.loads(raw)
    kind = install_kind() or ("windows-portable" if sys.platform == "win32" else "linux-deb")
    f = m.get("files", {}).get(kind)
    if not f:
        return None
    return UpdateInfo(m["version"], m.get("notes", ""), kind, f["name"], f["sha256"].lower(),
                      int(f["size"]), base + f["name"])


def download(info: UpdateInfo, dest_dir: Path, progress: Callable[[float], None],
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    """다운로드 + SHA-256/크기 검증. 실패하면 부분 파일을 지운다."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.name
    part = final.with_name(final.name + ".download")
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(info.url, headers={"User-Agent": f"CCTVViewer/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(part, "wb") as out:
            while chunk := r.read(256 * 1024):
                if cancelled():
                    raise UpdateError("취소했습니다")
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                progress(min(1.0, got / max(1, info.size)))
        if got != info.size or h.hexdigest() != info.sha256:
            raise UpdateError("다운로드한 파일의 검증값(SHA-256)이 맞지 않습니다")
        part.replace(final)
        return final
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def apply(info: UpdateInfo, path: Path) -> None:
    """받은 파일을 설치하고 새 버전을 실행한다. 호출 후 현재 앱은 종료해야 한다."""
    if info.kind == "windows-portable":
        # 실행 중인 exe 는 덮어쓸 수 없으므로 새 exe(버전이 다른 이름)를 옆에 두고 실행.
        # 새 버전이 시작하면서 --cleanup 으로 이전 exe 를 지운다. config.json 은 같은 폴더라 그대로 사용.
        old = Path(sys.executable).resolve()
        subprocess.Popen([str(path), "--cleanup", str(old)], cwd=str(path.parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    elif info.kind == "windows-setup":
        # 설치 프로그램이 실행 중인 뷰어를 닫고 설치한 뒤 다시 실행한다 (installer/CCTVViewer.iss [Run])
        subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    elif info.kind == "linux-deb":
        # pkexec: 관리자 암호 창 -> apt 로 설치(의존 패키지 포함). 끝나면 새 버전 실행.
        r = subprocess.run(["pkexec", "apt-get", "install", "-y", "--allow-downgrades", str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError("설치가 취소되었거나 실패했습니다" + (f": {r.stderr.strip()[-200:]}" if r.stderr else ""))
        subprocess.Popen(["/usr/bin/cctv-viewer"], start_new_session=True)
    else:
        raise UpdateError("이 실행 형태는 자동 업데이트를 지원하지 않습니다 (개발 실행)")


def download_dir(kind: str) -> Path:
    if kind == "windows-portable":
        return Path(sys.executable).resolve().parent  # exe 옆 (포터블 폴더)
    return Path(tempfile.gettempdir()) / "cctv-viewer-update"


def cleanup_old(path: str) -> None:
    """새 포터블 버전 시작 시 이전 exe 삭제 (이전 프로세스가 끝날 때까지 몇 초 재시도)."""
    import time
    p = Path(path)
    if p.resolve() == Path(sys.executable).resolve() or p.suffix.lower() != ".exe":
        return
    for _ in range(20):
        try:
            p.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.5)
    log.warning("이전 버전 파일을 지우지 못했습니다: %s", p)
