"""업데이트 창: 확인 -> 새 버전 안내 -> 다운로드(진행률) -> 설치/재시작."""
from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)

from . import __version__, updater


class _Bridge(QObject):
    checked = pyqtSignal(object, str)    # UpdateInfo | None, 오류 메시지
    progress = pyqtSignal(float)
    downloaded = pyqtSignal(object, str)  # Path | None, 오류 메시지


class UpdateDialog(QDialog):
    def __init__(self, update_url: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("업데이트")
        self.setMinimumWidth(520)
        self.update_url = update_url
        self.info: updater.UpdateInfo | None = None
        self._cancel = threading.Event()
        self._busy = False

        self.status = QLabel(f"현재 버전 v{__version__}\n업데이트 서버 확인 중…")
        self.status.setWordWrap(True)
        self.notes = QTextBrowser()
        self.notes.hide()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.go = QPushButton("업데이트")
        self.go.setDefault(True)
        self.go.hide()
        self.go.clicked.connect(self._download)
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.go)
        buttons.addWidget(self.close_btn)
        src = QLabel(f"서버: {update_url or '(미설정)'}")
        src.setStyleSheet("color: gray")
        src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.notes)
        lay.addWidget(self.bar)
        lay.addWidget(src)
        lay.addLayout(buttons)

        self._b = _Bridge()
        self._b.checked.connect(self._on_checked)
        self._b.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self._b.downloaded.connect(self._on_downloaded)
        threading.Thread(target=self._check_thread, daemon=True).start()

    # ---- 확인 ----
    def _check_thread(self) -> None:
        try:
            self._b.checked.emit(updater.check(self.update_url), "")
        except updater.UpdateError as e:
            self._b.checked.emit(None, str(e))
        except Exception as e:  # 예상 못한 오류도 창에 표시
            self._b.checked.emit(None, f"확인 실패 ({type(e).__name__})")

    def _on_checked(self, info, err: str) -> None:
        if err:
            self.status.setText(f"현재 버전 v{__version__}\n{err}")
            return
        if info is None:
            self.status.setText(f"현재 버전 v{__version__}\n이 설치 형태용 업데이트 파일이 서버에 없습니다.")
            return
        self.info = info
        if not info.newer:
            self.status.setText(f"현재 버전 v{__version__} — 최신 버전입니다. (서버 v{info.version})")
            return
        self.status.setText(f"새 버전 v{info.version} 이 있습니다. (현재 v{__version__}, {info.size / 1e6:.0f}MB)")
        if info.notes:
            self.notes.setPlainText(info.notes)
            self.notes.show()
        if updater.install_kind() is None:
            self.status.setText(self.status.text() + "\n개발 실행 중에는 자동 설치를 할 수 없습니다.")
            return
        self.go.show()

    # ---- 다운로드/설치 ----
    def _download(self) -> None:
        self._busy = True
        self.go.setEnabled(False)
        self.close_btn.setText("취소")
        self.bar.show()
        self.status.setText(f"v{self.info.version} 다운로드 중…")
        threading.Thread(target=self._download_thread, daemon=True).start()

    def _download_thread(self) -> None:
        try:
            path = updater.download(self.info, updater.download_dir(self.info.kind),
                                    self._b.progress.emit, self._cancel.is_set)
            self._b.downloaded.emit(path, "")
        except updater.UpdateError as e:
            self._b.downloaded.emit(None, str(e))
        except Exception as e:
            self._b.downloaded.emit(None, f"다운로드 실패 ({type(e).__name__})")

    def _on_downloaded(self, path, err: str) -> None:
        self._busy = False
        self.close_btn.setText("닫기")
        if err:
            self.bar.hide()
            self.go.setEnabled(True)
            self.status.setText(err)
            return
        self.status.setText("설치 중… 잠시 후 새 버전이 실행됩니다.")
        QApplication.processEvents()
        try:
            updater.apply(self.info, path)
        except updater.UpdateError as e:
            self.go.setEnabled(True)
            self.status.setText(str(e))
            return
        # 새 버전(또는 설치 프로그램)이 실행됐으므로 현재 앱 종료 (설정은 창 닫힐 때 저장됨)
        self.accept()
        for w in QApplication.topLevelWidgets():
            if w.inherits("QMainWindow"):
                w.close()
        QApplication.quit()

    def reject(self) -> None:
        if self._busy:
            self._cancel.set()
            return  # 다운로드 스레드가 취소를 알려오면 버튼이 '닫기' 로 돌아온다
        super().reject()
