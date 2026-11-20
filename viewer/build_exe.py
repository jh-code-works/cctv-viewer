"""CCTV Viewer 릴리스 빌드: 버전 자동 증가 + 설치 파일 + 업데이트 배포 파일 + git 태그/Gitea 릴리스.

    python build_exe.py                  # 패치 버전 +1 후 설치형(Setup.exe) 빌드 -> 커밋/태그/push/Gitea 릴리스
    python build_exe.py --notes "변경 내용"  # 업데이트 창에 보일 변경 내용 (생략 시 CHANGELOG '다음 버전' 제목)
    python build_exe.py --bump minor     # 1.0.2 -> 1.1.0   (major / none 도 가능)
    python build_exe.py --target portable|installer|deb|windows|all   (기본 installer)
    python build_exe.py --no-git         # git 커밋/태그/Gitea 릴리스 생략 (시험 빌드)
    python build_exe.py --init-update-key  # 최초 1회: 업데이트 서명 키 생성

git 연동 (저장소일 때): 커밋하지 않은 변경이 있으면 빌드하지 않는다 -> 배포본 = 태그된 코드.
CHANGELOG.md 의 '### 다음 버전(빌드 전) — 제목' 은 빌드 후 '### v{버전} — 제목' 으로 바뀐다.
롤백은 git_release.py rollback 참고.

결과물 (release/):
    CCTVViewer_v{버전}_Portable.exe   단일 파일. 설정은 exe 옆 config.json
    CCTVViewer_v{버전}_Setup.exe      설치 프로그램. 설정은 %APPDATA%\\CCTVViewer\\config.json
    cctv-viewer_{버전}_amd64.deb      Ubuntu 22.04/24.04. 설정은 ~/.config/cctv-viewer/config.json
    update/                           NAS web/cctv 폴더에 그대로 올릴 파일 (latest.json + 서명 + 설치 파일)

Ubuntu .deb 는 Windows 에서도 Docker(Ubuntu 22.04 컨테이너)로 빌드한다 (Docker Desktop 필요).

버전의 원본은 cctv_viewer/__init__.py 의 __version__ 하나뿐이다.
빌드가 실패하면 올렸던 버전을 원래대로 되돌린다.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "cctv_viewer" / "__init__.py"
ICON = ROOT / "assets" / "cctv.ico"
ISS = ROOT / "installer" / "CCTVViewer.iss"
# 리눅스 컨테이너에서는 빌드 중간 파일을 컨테이너 내부(/tmp)에 둔다 (Windows 공유 폴더는 느리고 권한/링크 제약)
_BUILD_ROOT = Path(os.environ.get("CCTV_BUILD_ROOT", ROOT))
BUILD = _BUILD_ROOT / "build"
DIST = _BUILD_ROOT / "dist"
RELEASE = ROOT / "release"
UPDATE_DIR = RELEASE / "update"
KEY_FILE = ROOT / "keys" / "update_ed25519.pem"
PUBKEY_MODULE = ROOT / "cctv_viewer" / "update_key.py"
APP = "CCTVViewer"
LINUX_APP = "cctv-viewer"
DEB_IMAGE = "ubuntu:22.04"

_VERSION_RE = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', re.M)

# 사용하지 않는 대형 Qt 모듈 제외 -> exe 크기/기동시간 감소
EXCLUDES = (
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml",
    "PyQt6.QtQuick", "PyQt6.QtMultimedia", "PyQt6.QtPdf", "PyQt6.QtBluetooth",
    "PyQt6.QtNetwork", "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner",
    "tkinter", "matplotlib", "cv2", "PIL",
)


# ---- 버전 ----------------------------------------------------------------------
def read_version() -> str:
    m = _VERSION_RE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"{VERSION_FILE} 에서 __version__ = \"X.Y.Z\" 를 찾을 수 없음")
    return ".".join(m.groups())


def bumped(version: str, part: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return version


def write_version(version: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    VERSION_FILE.write_text(_VERSION_RE.sub(f'__version__ = "{version}"', text), encoding="utf-8")


def write_version_resource(version: str) -> Path:
    """exe 파일 속성(자세히) 탭에 표시되는 Windows 버전 리소스."""
    v = tuple(map(int, version.split("."))) + (0,)
    path = BUILD / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('CompanyName', 'CCTV Viewer'),
      StringStruct('FileDescription', 'CCTV Viewer - Hikvision RTSP 뷰어'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{APP}'),
      StringStruct('OriginalFilename', '{APP}.exe'),
      StringStruct('ProductName', 'CCTV Viewer'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
""", encoding="utf-8")
    return path


# ---- 빌드 ----------------------------------------------------------------------
def ensure_icon() -> None:
    if not ICON.exists():
        subprocess.run([sys.executable, str(ROOT / "assets" / "make_icon.py")], check=True)


def pyinstaller(name: str, *, onefile: bool, portable: bool, console: bool, version_file: Path) -> None:
    import PyInstaller.__main__

    work = BUILD / name
    work.mkdir(parents=True, exist_ok=True)
    extra_data = [f"--add-data={ICON}{os.pathsep}assets",
                  f"--add-data={ROOT / 'assets' / 'models'}{os.pathsep}assets/models"]
    if portable:
        flag = work / "portable.flag"
        flag.write_text("portable", encoding="utf-8")
        extra_data.append(f"--add-data={flag}{os.pathsep}.")
    PyInstaller.__main__.run([
        str(ROOT / "main.py"),
        f"--name={name}",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        *([f"--icon={ICON}", f"--version-file={version_file}"] if sys.platform == "win32" else []),
        # PyAV 는 FFmpeg DLL(av.libs) 과 Cython 서브모듈이 많아 통째로 수집
        "--collect-all=av",
        # AI 감지: ONNX Runtime 네이티브 DLL
        "--collect-binaries=onnxruntime",
        "--hidden-import=onnxruntime",
        *extra_data,
        *(f"--exclude-module={m}" for m in EXCLUDES),
        f"--distpath={DIST}",
        f"--workpath={work}",
        f"--specpath={work}",
    ])


def build_portable(version: str, console: bool, version_file: Path) -> Path:
    pyinstaller(f"{APP}_portable", onefile=True, portable=True, console=console, version_file=version_file)
    out = RELEASE / f"{APP}_v{version}_Portable.exe"
    shutil.move(DIST / f"{APP}_portable.exe", out)
    return out


def find_iscc() -> Path:
    candidates = [
        os.environ.get("ISCC", ""),
        shutil.which("iscc") or "",
        rf"{os.environ.get('LOCALAPPDATA', '')}\Programs\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    sys.exit("Inno Setup 6(ISCC.exe) 를 찾을 수 없음.\n"
             "  winget install JRSoftware.InnoSetup  또는  ISCC 환경변수에 경로 지정")


def build_installer(version: str, console: bool, version_file: Path) -> Path:
    iscc = find_iscc()  # 오래 걸리는 PyInstaller 전에 먼저 확인
    # 설치형은 onedir: 실행 시 압축 해제가 없어 기동이 빠르고, 백신 오탐도 적다
    pyinstaller(APP, onefile=False, portable=False, console=console, version_file=version_file)
    subprocess.run([
        str(iscc), "/Q",
        f"/DAppVersion={version}",
        f"/DSourceDir={DIST / APP}",
        f"/DOutputDir={RELEASE}",
        str(ISS),
    ], check=True)
    return RELEASE / f"{APP}_v{version}_Setup.exe"


# ---- Ubuntu .deb ------------------------------------------------------------------
DEB_DEPENDS = [
    # PyQt6(Qt 6.5+) xcb 플랫폼 플러그인이 필요로 하는 시스템 라이브러리
    "libxcb-cursor0", "libxkbcommon-x11-0", "libxcb-icccm4", "libxcb-image0", "libxcb-keysyms1",
    "libxcb-randr0", "libxcb-render-util0", "libxcb-shape0", "libxcb-xinerama0", "libxcb-xkb1",
    "libegl1", "libgl1", "libfontconfig1", "libdbus-1-3", "libx11-xcb1", "libfreetype6",
    "libglib2.0-0t64 | libglib2.0-0",  # 24.04 부터 t64 이름 (22.04 는 libglib2.0-0)
    "fonts-noto-cjk",  # 한글 글꼴
]
DEB_RECOMMENDS = ["pkexec | policykit-1", "va-driver-all | mesa-va-drivers",
                  "pulseaudio-utils | pipewire-bin | alsa-utils"]  # 효과음 재생 (paplay / pw-play / aplay)  # 자동 업데이트 / VAAPI 하드웨어 디코딩


def build_deb_native(version: str) -> Path:
    """리눅스에서 실행: PyInstaller onedir -> /opt/cctv-viewer 로 설치되는 .deb 패키지."""
    import tempfile

    pyinstaller(LINUX_APP, onefile=False, portable=False, console=False, version_file=Path())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / f"{LINUX_APP}_{version}_amd64"
        opt = root / "opt" / LINUX_APP
        shutil.copytree(DIST / LINUX_APP, opt, symlinks=True)
        (root / "usr" / "bin").mkdir(parents=True)
        (root / "usr" / "bin" / LINUX_APP).symlink_to(f"/opt/{LINUX_APP}/{LINUX_APP}")
        icon_dir = root / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
        icon_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "assets" / "cctv.png", icon_dir / f"{LINUX_APP}.png")
        apps = root / "usr" / "share" / "applications"
        apps.mkdir(parents=True)
        (apps / f"{LINUX_APP}.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=CCTV Viewer\nName[ko]=CCTV 뷰어\n"
            "Comment=Hikvision RTSP CCTV viewer with AI detection\n"
            f"Exec={LINUX_APP}\nIcon={LINUX_APP}\nTerminal=false\nCategories=AudioVideo;Video;Security;\n",
            encoding="utf-8")
        debian = root / "DEBIAN"
        debian.mkdir()
        size_kb = sum(f.stat().st_size for f in opt.rglob("*") if f.is_file()) // 1024
        (debian / "control").write_text(
            f"Package: {LINUX_APP}\nVersion: {version}\nArchitecture: amd64\n"
            f"Maintainer: CCTV Viewer\nInstalled-Size: {size_kb}\nSection: video\nPriority: optional\n"
            f"Depends: {', '.join(DEB_DEPENDS)}\nRecommends: {', '.join(DEB_RECOMMENDS)}\n"
            "Description: Lightweight Hikvision RTSP CCTV viewer\n"
            " Low-CPU multi-channel RTSP viewer with jitter buffer, NVR playback,\n"
            " clip export and on-device AI person/vehicle detection.\n", encoding="utf-8")
        postinst = debian / "postinst"
        postinst.write_text("#!/bin/sh\nset -e\n"
                            "command -v update-desktop-database >/dev/null && update-desktop-database -q || true\n"
                            "command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true\n",
                            encoding="utf-8")
        postinst.chmod(0o755)
        out = RELEASE / f"{LINUX_APP}_{version}_amd64.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(out)], check=True)
    return out


def build_deb_docker(version: str) -> Path:
    """Windows 에서 실행: Ubuntu 22.04 컨테이너 안에서 build_deb_native 를 돌린다."""
    if not shutil.which("docker"):
        sys.exit("Ubuntu .deb 빌드에는 Docker 가 필요합니다 (Docker Desktop)")
    subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/src", "-v", "cctv-pip-cache:/root/.cache/pip", "-w", "/src",
        DEB_IMAGE, "bash", "/src/linux/docker_build.sh",
    ], check=True)
    out = RELEASE / f"{LINUX_APP}_{version}_amd64.deb"
    if not out.exists():
        sys.exit(f"컨테이너 빌드 후 {out.name} 이 없습니다")
    return out


# ---- 업데이트 배포 파일 (서명) -----------------------------------------------------
def init_update_key(force: bool) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64

    if KEY_FILE.exists() and not force:
        sys.exit(f"이미 키가 있습니다: {KEY_FILE}\n"
                 "키를 바꾸면 기존 설치본은 새 업데이트를 거부합니다. 정말 바꾸려면 --force-new-key")
    key = Ed25519PrivateKey.generate()
    KEY_FILE.parent.mkdir(exist_ok=True)
    KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                          serialization.PublicFormat.Raw)).decode()
    text = PUBKEY_MODULE.read_text(encoding="utf-8")
    PUBKEY_MODULE.write_text(re.sub(r'^PUBLIC_KEY = .*$', f'PUBLIC_KEY = "{pub}"', text, flags=re.M),
                             encoding="utf-8")
    print(f"업데이트 서명 키 생성:\n  개인 키 {KEY_FILE}  (백업해 두세요. 잃어버리면 기존 설치본에 업데이트를 배포할 수 없습니다)\n"
          f"  공개 키 -> {PUBKEY_MODULE.relative_to(ROOT)}")


def make_update_bundle(version: str, notes: str) -> Path:
    """release/update/ 에 NAS web/cctv 폴더로 그대로 올릴 파일을 만든다."""
    import base64
    import hashlib
    import json
    from datetime import datetime

    from cryptography.hazmat.primitives import serialization

    if not KEY_FILE.exists():
        sys.exit("업데이트 서명 키가 없습니다. 먼저: python build_exe.py --init-update-key")
    files = {
        "windows-portable": RELEASE / f"{APP}_v{version}_Portable.exe",
        "windows-setup": RELEASE / f"{APP}_v{version}_Setup.exe",
        "linux-deb": RELEASE / f"{LINUX_APP}_{version}_amd64.deb",
    }
    if UPDATE_DIR.exists():
        shutil.rmtree(UPDATE_DIR)
    UPDATE_DIR.mkdir(parents=True)
    manifest = {"version": version, "released": datetime.now().isoformat(timespec="seconds"),
                "notes": notes, "files": {}}
    for kind, path in files.items():
        if not path.exists():
            print(f"  (없음, 제외) {path.name}")
            continue
        shutil.copy2(path, UPDATE_DIR / path.name)
        manifest["files"][kind] = {"name": path.name, "size": path.stat().st_size,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    key = serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)
    (UPDATE_DIR / "latest.json").write_bytes(raw)
    (UPDATE_DIR / "latest.json.sig").write_bytes(base64.b64encode(key.sign(raw)))
    return UPDATE_DIR


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bump", choices=["patch", "minor", "major", "none"], default="patch")
    ap.add_argument("--set-version", metavar="X.Y.Z", help="버전을 직접 지정 (--bump 무시)")
    ap.add_argument("--target", choices=["portable", "installer", "deb", "windows", "all"], default="installer")
    ap.add_argument("--console", action="store_true", help="디버그용 콘솔 창 표시")
    ap.add_argument("--notes", default="", help="업데이트 창에 표시할 변경 내용")
    ap.add_argument("--no-bundle", action="store_true", help="release/update/ 배포 파일을 만들지 않음")
    ap.add_argument("--no-git", action="store_true", help="git 커밋/태그/Gitea 릴리스 생략 (시험 빌드)")
    ap.add_argument("--init-update-key", action="store_true", help="업데이트 서명 키 생성 (최초 1회)")
    ap.add_argument("--force-new-key", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.init_update_key:
        return init_update_key(args.force_new_key)
    if not args.no_bundle and not KEY_FILE.exists():
        sys.exit("업데이트 서명 키가 없습니다. 먼저: python build_exe.py --init-update-key")

    import git_release
    use_git = git_release.is_repo() and not args.no_git
    if use_git and (dirty := git_release.dirty_files()):
        sys.exit("커밋하지 않은 변경이 있어 릴리스하지 않습니다 (배포본과 git 기록을 일치시키기 위해).\n  "
                 + "\n  ".join(dirty[:20]) + "\n먼저 커밋하거나, 시험 빌드는 --no-git")
    if not args.notes:
        args.notes = pending_changelog_title()

    old = read_version()
    if args.set_version:
        if not re.fullmatch(r"\d+\.\d+\.\d+", args.set_version):
            sys.exit("--set-version 형식: X.Y.Z")
        new = args.set_version
    else:
        new = bumped(old, args.bump)
    print(f"버전: {old} -> {new}")
    write_version(new)

    try:
        ensure_icon()
        RELEASE.mkdir(exist_ok=True)
        version_file = write_version_resource(new)
        outputs = []
        on_windows = sys.platform == "win32"
        if on_windows and args.target in ("portable", "windows", "all"):
            outputs.append(build_portable(new, args.console, version_file))
        if on_windows and args.target in ("installer", "windows", "all"):
            outputs.append(build_installer(new, args.console, version_file))
        if args.target in ("deb", "all"):
            outputs.append(build_deb_docker(new) if on_windows else build_deb_native(new))
        if not args.no_bundle:
            outputs.append(make_update_bundle(new, args.notes))
    except BaseException:
        write_version(old)
        print(f"\n빌드 실패 -> 버전을 {old} 로 되돌림", file=sys.stderr)
        raise

    finalize_changelog(new)
    print("\n빌드 완료:")
    for o in outputs:
        if o.is_dir():
            print(f"  {o.relative_to(ROOT)}\\  <- 이 폴더의 파일을 NAS web/cctv 에 복사")
        else:
            print(f"  {o.relative_to(ROOT)}  ({o.stat().st_size / 1e6:.1f} MB)")
    if use_git:
        print("\ngit / Gitea:")
        files = [o for o in outputs if o.is_file() and o.suffix in (".exe", ".deb")]
        git_release.release(new, files, args.notes,
                            [str(VERSION_FILE.relative_to(git_release.REPO_ROOT)), "CHANGELOG.md"])


PENDING = "### 다음 버전(빌드 전)"


def pending_changelog_title() -> str:
    """CHANGELOG '### 다음 버전(빌드 전) — 제목' 의 제목 (업데이트 창 변경 내용 기본값)."""
    log = ROOT.parent / "CHANGELOG.md"
    if not log.exists():
        return ""
    m = re.search(rf"^{re.escape(PENDING)}\s*[—-]\s*(.+)$", log.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def finalize_changelog(version: str) -> None:
    """'다음 버전(빌드 전)' -> 'v{버전}', 상단 현재 버전/작업 기간 갱신."""
    from datetime import date
    log = ROOT.parent / "CHANGELOG.md"
    if not log.exists():
        return
    s = log.read_text(encoding="utf-8")
    today = date.today().isoformat()
    s = s.replace(PENDING, f"### v{version}", 1)
    s = re.sub(r"^- 현재 버전: \*\*v[\d.]+\*\* \([\d-]+\)", f"- 현재 버전: **v{version}** ({today})", s,
               count=1, flags=re.M)
    s = re.sub(r"^(- 작업 기간: [\d-]+) ~ [\d-]+ \((v[\d.]+) → v[\d.]+\)", rf"\1 ~ {today} (\2 → v{version})", s,
               count=1, flags=re.M)
    log.write_text(s, encoding="utf-8")


if __name__ == "__main__":
    main()
