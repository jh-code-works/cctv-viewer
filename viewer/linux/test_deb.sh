#!/bin/bash
# .deb 설치 검증: 깨끗한 Ubuntu 컨테이너에 설치 -> 자체 점검 -> 가상 화면(Xvfb)에서 실행/캡처.
#   docker run --rm -v "<viewer>:/src" ubuntu:24.04 bash /src/linux/test_deb.sh <deb 파일명> [config.json]
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
DEB="/src/release/$1"
apt-get update -qq
echo "== apt install $(basename "$DEB")"
apt-get install -y -qq "$DEB" >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 1; }
dpkg -s cctv-viewer | grep -E "^(Status|Version)"
ls -la /usr/bin/cctv-viewer /usr/share/applications/cctv-viewer.desktop
echo "== selftest"
QT_QPA_PLATFORM=offscreen cctv-viewer --selftest /tmp/st.txt && cat /tmp/st.txt
if [ -n "${2:-}" ]; then
  echo "== GUI (Xvfb) 15초 실행"
  apt-get install -y -qq xvfb x11-apps imagemagick >/dev/null 2>&1
  mkdir -p /root/.config/cctv-viewer && cp "/src/$2" /root/.config/cctv-viewer/config.json
  xvfb-run -s "-screen 0 1600x900x24" bash -c 'cctv-viewer & sleep 15; import -window root /src/release/linux_screen.png; kill %1' || true
  echo "config dir:"; ls /root/.config/cctv-viewer
  tail -5 /root/.config/cctv-viewer/cctv_viewer.log 2>/dev/null || true
fi
