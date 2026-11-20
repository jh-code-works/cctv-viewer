# 4~6채널 저부하 IP 카메라 CCTV 시스템

IE 전용 웹 플러그인 없이 RTSP를 지원하는 다양한 IP 카메라·NVR을 모니터링/녹화하는 두 가지 구성.

버전별 변경 내역·작업 기록·남은 과제: **[CHANGELOG.md](CHANGELOG.md)**

## 버전 관리 및 배포

릴리스 및 자동 업데이트(`build_exe.py` / `git_release.py`)를 자체 Git 저장소와 연동해 사용할 수 있습니다. (토큰은 OS 자격 증명 관리자에 저장하며 코드/설정에 넣지 않습니다.)

| 작업 | 방법 |
|---|---|
| 기능 수정 | 수정 → `CHANGELOG.md` 맨 위에 `### 다음 버전(빌드 전) — 제목` 절 작성 → 커밋 (`feat:` `fix:` `perf:` `docs:` `chore:`) |
| 배포 | `cd viewer` → `.venv\Scripts\python build_exe.py` → 버전 +1, Setup.exe 빌드, CHANGELOG 제목을 `v버전` 으로 확정, **커밋 + 태그 + push + Gitea 릴리스(설치 파일 첨부)** 까지 자동 → `release\update\` 를 NAS `web\cctv` 에 업로드 |
| 시험 빌드 | `build_exe.py --no-git` (커밋/태그/릴리스 안 함) |
| 아직 배포 안 한 커밋 취소 | `git revert <커밋>` |
| **배포한 버전 롤백** | `.venv\Scripts\python git_release.py rollback v1.0.19` → 앱 코드를 v1.0.19 로 되돌리는 커밋 → `build_exe.py` 로 **새 버전(예: v1.0.21)으로 배포** |
| 급하게 PC 한 대만 | Gitea 릴리스 페이지에서 이전 버전 Setup.exe 를 받아 설치 |

- 커밋하지 않은 변경이 있으면 `build_exe.py` 가 빌드를 거부합니다 → 배포본은 항상 태그된 코드와 같습니다.
- 자동 업데이트는 **더 높은 버전으로만** 올라갑니다. 예전 설치 파일을 NAS 에 다시 올려도 PC 들은 받지 않으므로, 롤백은 "예전 코드를 담은 새 버전" 으로 배포합니다. 롤백해도 버전 번호·CHANGELOG·빌드 도구는 되돌리지 않습니다.
- git 에 넣지 않는 것: 업데이트 서명 키(`viewer/keys/`), 카메라 비밀번호가 든 `config.json`, `nvr/.env`, 설치 파일(`release/`, Gitea 릴리스에 보관), `.venv`

```
[IP 카메라 4~6대] ──(메인 1 + 서브 1 연결)──> [ go2rtc ]  패킷 복사 릴레이, CPU ~0
                                              ├──> [ Frigate ]  sub: 객체 감지 / main: 녹화(-c copy)
                                              └──> [ PC 뷰어(Part 1) ]
```

| 폴더 | 내용 |
|---|---|
| `viewer/` | **Part 1** PyQt6 + PyAV 데스크톱 뷰어 + AI 사람/차량 감지 (단독 사용 또는 go2rtc 경유) |
| `nvr/` | **Part 2** go2rtc + Frigate Docker Compose |

---

## Part 1. PC 뷰어 (`viewer/`)

### 실행
```powershell
cd viewer
python -m venv .venv; .\.venv\Scriptsctivate
pip install -r requirements.txt
python main.py                 # 첫 실행 시 config.json 생성 → [설정]에서 카메라 입력
```

### 릴리스 빌드 (포터블 + 설치형)
설치형을 빌드하려면 Inno Setup 6 이 필요합니다: `winget install JRSoftware.InnoSetup`

```powershell
python build_exe.py                    # 패치 버전 +1 (1.0.1 → 1.0.2) 후 둘 다 빌드
python build_exe.py --bump minor       # 1.0.2 → 1.1.0   (major: 2.0.0 / none: 버전 유지)
python build_exe.py --target portable  # 포터블만 (installer / all)
```

| 결과물 (`viewer/release/`) | 형태 | 설정 파일 위치 |
|---|---|---|
| `CCTVViewer_v1.0.1_Portable.exe` | 단일 exe, 설치 없이 실행 (USB 가능) | exe 옆 `config.json` |
| `CCTVViewer_v1.0.1_Setup.exe` | 설치 프로그램 (시작 메뉴, 바탕화면 아이콘, 자동 실행 선택, 제거 지원) | `%APPDATA%\CCTVViewer\config.json` |

- 버전의 원본은 `cctv_viewer/__init__.py` 의 `__version__` 하나입니다. 빌드할 때마다 자동으로 올라가고,
  빌드가 실패하면 원래 버전으로 되돌아갑니다. 버전은 파일 이름, 창 제목, exe 속성(자세히 탭), 프로그램 추가/제거에 표시됩니다.
- 새 버전 Setup 을 실행하면 기존 설치를 그 자리에서 업그레이드합니다 (설정 유지). 설치할 때 '모든 사용자' 와 '현재 사용자만' 중에서 고를 수 있습니다.
- 제거할 때 카메라 설정(비밀번호 포함)도 지울지 묻습니다.
- 아이콘은 `assets/make_icon.py` 로 생성합니다 (`assets/cctv.ico`, 16~256px).

`config.json` 에는 **비밀번호가 평문으로 저장**되므로 뷰어 전용 저권한 계정(라이브뷰 권한만 있는 운영자 계정)을 만들어 쓰세요.

### 조작
| 동작 | 키 |
|---|---|
| 셀 더블클릭 | 해당 카메라 1채널 확대 (설정한 `확대 화면 해상도`로 전환) / 다시 더블클릭·`Esc` 로 복귀 |
| 2x2 / 3x2 / 3x3 | `Ctrl+2` / `Ctrl+6` / `Ctrl+3` (3x2 는 6대가 한 화면. 2x2 에서 5~6번 카메라는 `PgDn` 으로 페이지 전환) |
| 해상도·FPS·디코더·버퍼 지연 표시 | `I` |
| 창 전체화면 | `F11` |

### 저부하 설계
| 기법 | 구현 위치 | 효과 |
|---|---|---|
| 해상도 직접 선택: 그리드 / 확대 화면 각각 메인(101, 고화질) 또는 서브(102, 저부하). 창 크기와 무관하게 고정 | `main_window._update_streams()` | 설정 → `그리드 해상도`, `확대 화면 해상도` (기본: 둘 다 메인) |
| swscale 변환기 재사용 + BICUBIC/AREA 스케일 | `StreamWorker._convert()` | 1080p 변환 13~15ms → 1~2ms, 화질 향상 |
| **화면에 안 보이는 카메라는 연결 해제** | `main_window.apply()` | 확대 중엔 나머지 5채널 디코딩 0 |
| 최신 프레임 1장 슬롯 (buffer=1) | `StreamWorker.take_frame()` | GUI 가 못 따라오면 색변환 전에 드롭 |
| 색변환+축소를 swscale 한 번에, 셀 크기로 | `StreamWorker._convert()` | 4K→셀 크기, Qt 재스케일 없음 |
| FPS 상한 (그리드 15 / 확대 30) | `grid_max_fps` | 초과 프레임은 변환 없이 폐기 |
| **적응형 지터 버퍼** (압축 패킷을 흔들림만큼만 모았다가 카메라 PTS 대로 재생) | `JitterBuffer` | 무선 카메라의 몰림 도착으로 인한 끊김 제거. 설정 `끊김 보정 버퍼` (기본 최대 2000ms, 0=끄기) |
| 적체 감지 → 다음 키프레임까지 스킵 | `LAG_SKIP_MARGIN` | 버퍼가 한도+2초를 넘으면 실시간 복귀 |
| `nobuffer`, `low_delay`, `reorder_queue_size=0` | `StreamWorker._open()` | 수신 지연 최소화 |
| HW 디코딩 `auto` = d3d11va → dxva2 → cuda → qsv 실제 생성 테스트 | `resolve_hwaccel()` | 드라이버 없으면 자동 SW |

HW 디코딩은 기본적으로 **메인스트림에만** 적용합니다. 360p 서브스트림은 GPU→RAM 복사 비용이
SW 디코딩보다 커지는 경우가 많습니다. (설정에서 서브스트림에도 켤 수 있음)

**측정값** (8코어 PC, NVR 4채널 2x2, 1920x1040 창): 메인스트림 1080p + d3d11va **CPU 7.7%, RAM 308MB, 16fps** / 서브스트림 **CPU 2.4%, RAM 157MB**.
실제 카메라 영상은 움직임과 비트레이트가 더 커서 수치가 올라가니, 현장에서 `I` 키로 확인하세요.

### Ubuntu 설치 (22.04 / 24.04, amd64)
```bash
sudo apt install ./cctv-viewer_1.0.13_amd64.deb   # 필요한 라이브러리·한글 글꼴까지 자동 설치
cctv-viewer                                         # 또는 앱 메뉴의 "CCTV 뷰어"
sudo apt remove cctv-viewer                         # 제거
```
- 설치 위치 `/opt/cctv-viewer`, 설정·로그·스냅샷·클립은 `~/.config/cctv-viewer/`
- 하드웨어 디코딩은 VAAPI(Intel/AMD) 자동 사용 (`va-driver-all` 권장 패키지)
- .deb 는 Windows 에서 Docker(Ubuntu 22.04 컨테이너)로 빌드: `python build_exe.py --target deb`
- 설치 검증: `docker run --rm -v "<viewer 폴더>:/src" ubuntu:24.04 bash /src/linux/test_deb.sh cctv-viewer_<버전>_amd64.deb`

### 업데이트 (설정 → [업데이트 확인…])
`config.json`의 `update_url`에 업데이트 서버 주소(예: `https://example.com/cctv/`)를 설정하면 자동 업데이트를 사용할 수 있습니다.

| 설치 형태 | 동작 |
|---|---|
| Windows 포터블 | 새 exe 를 같은 폴더에 받아 실행 → 이전 exe 자동 삭제 (config.json 유지) |
| Windows 설치형 | 설치 프로그램을 조용히 실행 → 설치 후 새 버전 자동 실행 |
| Ubuntu | 관리자 암호 확인(pkexec) → `apt` 로 새 .deb 설치 → 새 버전 실행 |

**배포 절차**
1. (최초 1회) `python build_exe.py --init-update-key` → 서명 키 생성. **`viewer\keys\update_ed25519.pem` 은 반드시 백업** (잃어버리면 이미 설치된 뷰어에 업데이트를 보낼 수 없음. 절대 공유/업로드 금지)
2. `python build_exe.py --notes "변경 내용"` → 버전 +1, Windows/Ubuntu 빌드, `release\update\` 생성
3. `release\update\` 의 파일 전부(`latest.json`, `latest.json.sig`, 설치 파일 3개)를 NAS `web\cctv` 에 복사 (File Station). 이전 버전 설치 파일은 지워도 됨

**보안**: `latest.json` 은 Ed25519 로 서명되고 각 설치 파일은 SHA-256 으로 검증됩니다. NAS 파일이 바뀌어도 서명 키 없이는 설치되지 않습니다. 관리용으로 `CCTVViewer.exe --update-now` (창 없이 업데이트, 결과는 `update.log`)도 있습니다.

### AI 감지 (사람 / 차량 / 동물)
툴바 **AI 감지**(`Ctrl+D`)로 켜고 끕니다. 오른쪽 **이벤트 목록**(`Ctrl+E`)에 썸네일과 함께 기록됩니다.

- **이벤트 더블클릭 / [녹화 보기]** → NVR 하드디스크의 녹화를 **감지 10초 전부터** 재생 (◀30초·◀5초·5초▶·30초▶ 이동). `rtsp://…/Streaming/tracks/{채널}01?starttime=…` 사용, PC 에는 영상을 저장하지 않음. NVR 이 덮어쓰기 전까지(용량에 따라 약 2~3주) 재생 가능
- **PC에 저장 (수동)**: 재생 창의 **[💾 PC에 저장]** 또는 이벤트 우클릭 → 시작 시각·길이(5~600초)·저장 위치를 정하고 **[저장]** 을 눌렀을 때만 NVR 녹화를 MP4 로 받음. 재인코딩 없이 원본(H.265) 그대로라 화질 손실·CPU 부담 없음. NVR 이 실제 속도로 보내므로 30초 구간 ≈ 30초 소요. 기본 위치 `config.json` 옆 `clips\날짜\시각_카메라.mp4`
- **우클릭** → 라이브 확대 / PC에 저장 / 스냅샷 열기
- 이벤트 목록은 뷰어를 끄면 비워집니다 (스냅샷 파일은 유지)

| 항목 | 내용 |
|---|---|
| 모델 | YOLOX-Nano(기본, 빠름) / YOLOX-Tiny(정확, 먼 거리 유리). Apache-2.0, ONNX Runtime (Windows: GPU DirectML 우선 + CPU 자동 폴백, Linux: CPU) |
| 부하 절감 | 카메라당 초당 3장만 분석 → 64x36 흑백 비교로 **움직임이 있을 때만** AI 실행 (실측: 분석 기회의 83~93% 건너뜀) |
| 이벤트 = **움직임 확정** | 2회 이상 연속 감지 + 처음 위치에서 실제로 이동한 대상만 이벤트 (Frigate 방식). 드럼통·잡동사니를 차량으로 잘못 잡아도 제자리면 무시(흐린 점선 "(정지)"), 주차된 차는 출발할 때 이벤트. 실측: 실제 카메라 오탐 이벤트 0건 / 걷는 사람 약 1초 내 확정 |
| 재등장 | 확정된 대상이 `이벤트 간격`(기본 15초) 이상 안 보였다가 다시 나타나면 새 이벤트 |
| **감지 시 자동 확대** | 사람·차량 움직임이 확정된 채널을 확대 화면 해상도(기본 메인)로 `3초` 보여준 뒤 원래 화면으로 복귀. 3초는 메인 영상이 실제로 뜬 시점부터 계산(연결 최대 6초 대기). 확대 중에도 다른 채널 연결·AI 감지 유지 → 복귀 즉시. 직접 확대해 보고 있을 때는 동작 안 함. 연속 감지 시 최신 채널로 전환 |
| 스냅샷 | `config.json` 옆 `snapshots\날짜\시각_카메라_대상.jpg` (박스 포함) |
| 측정 | 실제 NVR 3채널 + 테스트 영상 1채널, 1080p: CPU +1.9%p, RAM +84MB |

- **화면에 표시 중인 카메라만** 분석합니다. 확대 중이면 그 카메라만 분석합니다.
- 오탐이 많으면 설정의 `신뢰도 기준`을 올리세요(기본 60%). 멀리 있는 사람을 놓치면 모델을 "정확"으로 바꾸세요.
- 자동 확대: 설정 → AI 감지 → `감지 시 자동 확대` (사용 / 시간 1~30초 / 대상 사람·차량·동물)
- **감지 제외 영역 (펜)**: 카메라 화면 **우클릭 → 감지 제외 영역 그리기** → 오두막·천막·드럼통처럼 잘못 인식되는 곳이나 흔들리는 나무를 펜으로 칠함(오른쪽 버튼 = 지우개). 물체의 발 위치(박스 아래 중앙)가 칠한 곳이거나 박스의 85% 이상이 칠한 곳이면 제외 → 오두막 앞을 지나가는 사람은 계속 감지. 칠한 곳의 움직임으로는 AI 를 돌리지 않아 CPU 도 절약. `I`(정보 표시)를 켜면 칠한 곳이 반투명 빨강으로 보임
- 제자리 물체(움직임 미확정) 박스는 기본 숨김 → 설정 `제자리 물체 박스도 표시`로 켤 수 있음
- 모델 비교(실측, 4채널 메인·저녁): AI 끔 6.7% / 빠름(nano) 10.4% / 정확(tiny) 15.4% CPU. 정확 모델이 야간 적외선 오탐이 적음
- 효과음: 움직임이 확정되면 1회 재생 (동시 감지여도 1.5초 안에 한 번). 설정 → `움직임 감지 소리`에서 딩동/삐/삐삐, 음량, [미리 듣기]. 소리 파일 없이 파형을 직접 생성 (Windows: winsound, Ubuntu: paplay/pw-play/aplay)
- 스트림 전환(서브↔메인, 확대/복귀)은 **끊김 없이**: 새 스트림 첫 화면이 올 때까지 기존 영상이 계속 재생됩니다 (무선 카메라는 새 연결에 1~5초)
- 배포본 점검: `CCTVViewer.exe --selftest 결과.txt` 를 실행하면 하드웨어 가속과 AI 모델 로드 결과가 파일로 저장됩니다.

### go2rtc 경유 모드
설정의 **go2rtc 서버** 를 켜면 카메라 대신 `rtsp://relay:***@서버:8554/camN_main|sub` 로 접속합니다.
대부분의 IP 카메라·NVR은 동시 RTSP 세션 수 제한이 있으므로, PC 여러 대와 Frigate 가 동시에 붙을 때는 이 모드를 권장합니다.

---

## Part 2. NVR (`nvr/`)

```bash
cd nvr
cp .env.example .env      # 카메라 IP, 계정, 릴레이/관리 비밀번호 수정
docker compose up -d                          # go2rtc
docker compose --profile frigate up -d        # Frigate 까지 (선택)
docker logs frigate 2>&1 | grep -i password   # Frigate 최초 admin 비밀번호
```

| 서비스 | 포트 | 용도 |
|---|---|---|
| go2rtc | 8554 | RTSP 재송출 (인증 필수: `RELAY_USER/PASS`) |
| go2rtc | 8555 tcp/udp | WebRTC |
| go2rtc | 127.0.0.1:1984 | 웹 UI/API (인증 + **로컬 전용 바인딩**) |
| Frigate | 8971 | 웹 UI (HTTPS + 로그인) |

**배치 전 확인할 것**
1. **카메라 서브스트림 해상도**를 `frigate/config/config.yml` 의 `detect.width/height` 와 맞춥니다 (기본 640x360).
2. 브라우저 WebRTC 는 **H.265 를 지원하지 않습니다.** 웹/모바일로 보려면 카메라 코덱을 H.264 로 두세요 (PC 뷰어와 Frigate 녹화는 H.265 도 가능).
3. 감지기는 호환성을 위해 `openvino` + `device: CPU` 가 기본값입니다. Linux + Intel iGPU 에서는
   `device: GPU`, `hwaccel_args: preset-vaapi`, compose 의 `/dev/dri` 를 켜면 CPU 부하가 더 줄어듭니다.
   Windows Docker Desktop(WSL2)에서는 `/dev/dri` 를 쓸 수 없어 CPU 모드로만 동작합니다.
4. go2rtc 는 검증한 1.9.14 로 고정돼 있습니다. `frigate:stable` 도 운영 전에 특정 버전으로 고정하세요.

**보안**
- go2rtc API 는 `exec:` 소스(셸 명령)를 지원하므로 **1984 를 무인증으로 노출하면 원격 명령 실행 경로가 됩니다.**
  API 인증과 로컬 바인딩을 유지하고, 원격 관리가 필요하면 SSH 터널이나 VPN 을 쓰세요.
- 카메라는 가능하면 별도 VLAN 에 두고 go2rtc 서버만 접근하도록 막으면, PC 가 카메라에 직접 붙지 않습니다.
- `.env` 는 `.gitignore` 에 포함돼 있습니다. 비밀번호의 `@ : / # %` 는 URL 인코딩이 필요합니다 (`@` → `%40`).
- Frigate 5000 포트(무인증 내부 API)는 외부에 열지 않습니다.

---

## 라이선스 (License)

이 프로젝트는 **[MIT License](LICENSE)** 를 따릅니다.  
누구나 자유롭게 사용·복제·수정·배포할 수 있으나, **모든 복제본 및 파생 저작물에 원저작권 표시(`Copyright (c) 2026 jh-code-works`)와 출처(`https://github.com/jh-code-works/cctv-viewer`) 및 라이선스 전문을 반드시 포함**해야 합니다.
