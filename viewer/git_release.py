"""git 커밋/태그 + Gitea 릴리스(설치 파일 첨부) + 롤백. build_exe.py 가 빌드 성공 후 release() 를 호출한다.

    python git_release.py publish v1.0.19 release/CCTVViewer_v1.0.19_Setup.exe  # 기존 태그에 릴리스만 다시 만들기
    python git_release.py rollback v1.0.19   # 코드를 v1.0.19 상태로 되돌리는 커밋 -> 이어서 build_exe.py

롤백 = '예전 코드를 담은 새 버전' 배포. 자동 업데이트는 더 높은 버전으로만 올라가므로 예전 설치 파일을
NAS 에 다시 올려서는 PC 들이 받지 않는다. rollback 은 버전 번호(__init__.py)는 그대로 두고 코드만 되돌린다.

설치 파일은 git 에 넣지 않고(수백 MB) Gitea 릴리스에 첨부해 버전별로 보관한다 -> 롤백 시 이전 설치 파일을 바로 받음.
토큰은 코드/설정에 두지 않고 git 자격 증명 관리자(git credential)에서 읽는다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패:\n{r.stderr.strip()}")
    return r.stdout.rstrip()  # 앞 공백 유지: status --porcelain 의 " M 파일" 형식


def is_repo() -> bool:
    return (REPO_ROOT / ".git").exists()


def dirty_files(allowed: set[str] = frozenset()) -> list[str]:
    """커밋되지 않은 변경 (allowed 경로는 제외). 릴리스는 커밋된 코드로만 만든다."""
    out = git("status", "--porcelain")
    files = [line[3:].strip('"') for line in out.splitlines() if line]
    return [f for f in files if f not in allowed]


def _remote() -> tuple[str, str]:
    """origin URL -> (https://host, owner/repo)"""
    url = git("remote", "get-url", "origin")
    m = re.match(r"(https?://[^/]+)/(.+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"origin 주소를 해석할 수 없음: {url}")
    return m.group(1), m.group(2)


def _token(base: str) -> str:
    host = base.split("://", 1)[1]
    out = subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
                         capture_output=True, text=True, cwd=REPO_ROOT, timeout=30).stdout
    m = re.search(r"^password=(.+)$", out, re.M)
    if not m:
        raise RuntimeError("Gitea 토큰이 git 자격 증명에 없습니다")
    return m.group(1).strip()


def _api(method: str, path: str, body=None, *, data: bytes | None = None, ctype: str = "application/json"):
    base, repo = _remote()
    req = urllib.request.Request(f"{base}/api/v1/repos/{repo}{path}", method=method,
                                 data=data if data is not None else (json.dumps(body).encode() if body else None))
    req.add_header("Authorization", f"token {_token(base)}")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"Gitea API {method} {path}: {e.code} {e.read()[:300]!r}") from None


def changelog_section(version: str) -> str:
    """CHANGELOG.md 의 '### v1.0.19 — ...' 절 본문 (릴리스 설명용)."""
    if not CHANGELOG.exists():
        return ""
    text = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(rf"^### v{re.escape(version)}\b.*?$(.*?)(?=^### |^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def changelog_title(version: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    m = re.search(rf"^### v{re.escape(version)}\s*[—-]\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def publish(tag: str, title: str, body: str, files: list[Path]) -> str:
    """태그에 Gitea 릴리스를 만들고(있으면 재사용) 파일을 첨부. 릴리스 페이지 URL 반환."""
    rel = _api("GET", f"/releases/tags/{tag}")
    if rel is None:
        rel = _api("POST", "/releases", {"tag_name": tag, "name": f"{tag} — {title}" if title else tag, "body": body})
    existing = {a["name"] for a in rel.get("assets", [])}
    for f in files:
        if f.name in existing:
            continue
        boundary = uuid.uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{f.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        _api("POST", f"/releases/{rel['id']}/assets?name={f.name}", data=data,
             ctype=f"multipart/form-data; boundary={boundary}")
        print(f"  릴리스 첨부: {f.name} ({f.stat().st_size / 1e6:.1f} MB)")
    return rel["html_url"]


def release(version: str, files: list[Path], notes: str, paths: list[str]) -> None:
    """버전 파일 등 빌드가 바꾼 파일을 커밋 -> 태그 -> push -> Gitea 릴리스."""
    tag = f"v{version}"
    git("add", *paths)
    if git("diff", "--cached", "--name-only"):
        git("commit", "-m", f"release: {tag}\n\n{notes}".strip()
            + "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    title = changelog_title(version) or notes
    git("tag", "-a", tag, "-m", f"{tag} — {title}" if title else tag)
    git("push", "origin", "HEAD", tag)
    print(f"  git: 커밋/태그 {tag} push 완료")
    body = changelog_section(version) or notes
    url = publish(tag, title, body, files)
    print(f"  Gitea 릴리스: {url}")


VERSION_PATH = "viewer/cctv_viewer/__init__.py"
# 롤백해도 그대로 두는 파일: 버전 번호/기록은 앞으로만 가고, 빌드·배포 도구는 최신이어야 롤백 버전을 빌드할 수 있다
KEEP = (VERSION_PATH, "CHANGELOG.md", "README.md", "viewer/build_exe.py", "viewer/git_release.py", ".gitignore", ".gitattributes")
SECTION = "## 1. 버전별 변경 내역\n\n"


def rollback(tag: str) -> None:
    """tag 이후의 앱 코드 변경을 모두 되돌리는 커밋 1개. push/배포는 이어서 build_exe.py 가 한다."""
    if dirty_files():
        sys.exit("커밋하지 않은 변경이 있습니다. 먼저 커밋하거나 정리하세요.")
    git("rev-parse", "--verify", f"{tag}^{{commit}}")
    if not git("rev-list", f"{tag}..HEAD"):
        sys.exit(f"{tag} 이후 변경이 없습니다.")
    current = git("describe", "--tags", "--abbrev=0")
    git("checkout", tag, "--", ".")
    git("checkout", "HEAD", "--", *[k for k in KEEP if git("ls-files", k)])
    for f in git("diff", "--name-only", "--diff-filter=A", tag, "HEAD").splitlines():  # tag 이후 새로 생긴 파일
        if f not in KEEP:
            git("rm", "-q", "--", f)
    entry = (f"### 다음 버전(빌드 전) — {tag} 로 롤백\n"
             f"- {current} 에서 문제가 있어 앱 코드를 {tag} 상태로 되돌림 "
             "(버전 번호는 계속 증가: 자동 업데이트는 낮은 버전으로 내려가지 않음)\n\n")
    text = CHANGELOG.read_text(encoding="utf-8")
    CHANGELOG.write_text(text.replace(SECTION, SECTION + entry, 1), encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", f"revert: {tag} 상태로 롤백 ({current} 이후 변경 되돌림)\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    print(f"롤백 커밋 완료: {git('log', '--oneline', '-1')}\n다음: python build_exe.py  (새 버전으로 배포)")


if __name__ == "__main__":
    cmd, tag, *paths = sys.argv[1:]
    if cmd == "rollback":
        rollback(tag)
    else:
        ver = tag.lstrip("v")
        print(publish(tag, changelog_title(ver), changelog_section(ver), [Path(p) for p in paths]))
