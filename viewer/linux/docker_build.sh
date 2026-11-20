#!/bin/bash
# Ubuntu 22.04 컨테이너 안에서 .deb 빌드 (build_exe.py --target deb 가 호출).
# 22.04(glibc 2.35)에서 빌드하면 22.04 / 24.04 모두에서 실행된다.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev binutils dpkg-dev ca-certificates \
    libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libegl1 libgl1 libfontconfig1 libdbus-1-3 \
    libglib2.0-0 libx11-xcb1 libfreetype6 \
    >/dev/null
python3 -m venv /opt/venv
. /opt/venv/bin/activate
pip install -q --upgrade pip
pip install -q -r /src/requirements.txt
# PyInstaller 의 PyQt6 훅은 빌드 중 QtGui 를 실제로 import 해서 Qt 플러그인(xcb 등)을 수집한다.
# 시스템 라이브러리(특히 libglib)가 없으면 경고만 내고 플러그인을 빠뜨려 설치 후 창이 뜨지 않으므로 미리 확인.
python -c "from PyQt6 import QtGui, QtWidgets" || { echo "PyQt6 import 실패: 시스템 라이브러리 누락"; exit 1; }
cd /src
CCTV_BUILD_ROOT=/tmp/cctvbuild python build_exe.py --bump none --target deb --no-bundle
